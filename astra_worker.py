"""
Опрос Mango + доставка дайджестов для Astra-конвейера. Сам разбор звонка
(транскрибация, оценка, немедленная доставка) больше не выполняется в этом
процессе — с Этапа 1 мультиагентной шины это обработчик очереди
`analyze_call` (handlers/analyze_call.py), который забирает задачи через
queue_runner.py. Здесь остаётся только: опрос Mango и постановка задач в
очередь, heartbeat, sweep таймаутов записи, вечерний дайджест.

Источник звонков — ОПРОС Mango (poll_client_calls), не вебхуки — push-вебхуки
у Mango платные, базовый API (stats/calls/request + result) бесплатный и уже
используется.

Опрос — раз в client.mango_poll_interval_sec (900 сек = 15 минут). После
каждого цикла опроса — heartbeat-сообщение РОПу в Telegram: воркер реально
проверяет Mango, а не тихо стоит (см. историю с часовым поясом — опрос молчал
сутками, ничего не показывая в логах как сломанное).
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot

import mango_client
from analysis import CRITERIA
from crypto_util import decrypt
from reports import esc, send_long

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("callbot-astra-worker")

bot = Bot(token=os.environ["BOT_TOKEN"])

POLL_INTERVAL_SEC = 5
SWEEP_INTERVAL_SEC = 60
DIGEST_GRACE_HOURS = 2

POLL_OVERLAP_MIN = 3
INITIAL_LOOKBACK_MIN = 60
MAX_LOOKBACK_DAYS = 25
STATS_RESULT_MAX_ATTEMPTS = 10
STATS_RESULT_RETRY_SEC = 2


def _day_bounds(client: asyncpg.Record, offset_days: int = 0) -> tuple[datetime, datetime]:
    tz = ZoneInfo(client["timezone"])
    day = datetime.now(tz).date() + timedelta(days=offset_days)
    start = datetime.combine(day, dtime.min, tzinfo=tz)
    return start, start + timedelta(days=1)


async def _head_chat_id(pool: asyncpg.Pool, client_id: int) -> int | None:
    row = await pool.fetchrow("SELECT telegram_user_id FROM employees WHERE client_id=$1 AND role='head'", client_id)
    return row["telegram_user_id"] if row else None


# -------------------------------------------------------------------- опрос

def _employee_leg(call: dict) -> dict | None:
    for leg in call.get("context_calls") or []:
        if leg.get("call_abonent_extension"):
            return leg
    return None


async def _fetch_calls_window(vpbx_api_key: str, salt: str, start: datetime, end: datetime) -> list[dict]:
    fmt = "%d.%m.%Y %H:%M:%S"
    req = await mango_client.call("stats/calls/request", vpbx_api_key, salt, {
        "start_date": start.strftime(fmt), "end_date": end.strftime(fmt),
        "limit": "2000", "offset": "0",
    })
    key = req.get("key")
    if not key:
        raise RuntimeError(f"stats/calls/request без key: {req}")

    for _ in range(STATS_RESULT_MAX_ATTEMPTS):
        res = await mango_client.call("stats/calls/result", vpbx_api_key, salt, {"key": key})
        status = res.get("status")
        if status == "complete":
            data = res.get("data") or []
            payload = (data[0] if data else {}) if isinstance(data, list) else data
            return payload.get("list", [])
        if status in ("error", "not-found", "cancel"):
            raise RuntimeError(f"stats/calls/result статус={status}: {res}")
        await asyncio.sleep(STATS_RESULT_RETRY_SEC)
    raise RuntimeError("stats/calls/result не дождались 'complete'")


async def _enqueue_analyze_call(pool: asyncpg.Pool, client_id: int, call_row_id: int, mango_entry_id: str) -> None:
    """Ключ дедупликации — идентификатор звонка в Манго: при пересечении окон
    опроса (POLL_OVERLAP_MIN) один и тот же звонок может встретиться повторно,
    но задача для него будет поставлена только один раз."""
    await pool.execute(
        """
        INSERT INTO tasks (type, client_id, input, dedup_key)
        VALUES ('analyze_call', $1, $2::jsonb, $3)
        ON CONFLICT (type, dedup_key) DO NOTHING
        """,
        client_id, json.dumps({"call_id": call_row_id}), mango_entry_id,
    )


async def poll_client_calls(pool: asyncpg.Pool, client: asyncpg.Record) -> tuple[int, int]:
    """Возвращает (найдено всего, пойдёт в разбор) — для heartbeat-сообщения."""
    tz = ZoneInfo(client["timezone"])
    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc.astimezone(tz).replace(tzinfo=None)
    start_msk = (
        (client["last_call_synced_at"].astimezone(tz).replace(tzinfo=None) - timedelta(minutes=POLL_OVERLAP_MIN))
        if client["last_call_synced_at"] else now_msk - timedelta(minutes=INITIAL_LOOKBACK_MIN)
    )
    start_msk = max(start_msk, now_msk - timedelta(days=MAX_LOOKBACK_DAYS))

    vpbx_api_key = decrypt(client["vpbx_api_key_enc"])
    vpbx_api_salt = decrypt(client["vpbx_api_salt_enc"])
    calls = await _fetch_calls_window(vpbx_api_key, vpbx_api_salt, start_msk, now_msk)

    to_analyze = 0
    for c in calls:
        entry_id = c.get("entry_id")
        if not entry_id:
            continue
        leg = _employee_leg(c)
        extension = leg.get("call_abonent_extension") if leg else None
        direction = c.get("context_type")
        duration = c.get("talk_duration") or 0
        client_number = c.get("called_number") if direction == 2 else c.get("caller_number")
        recording_ids = (leg.get("recording_id") if leg else None) or []
        recording_id = recording_ids[0] if recording_ids else None
        started_raw = c.get("context_start_time")
        call_started_at = datetime.fromtimestamp(started_raw, tz=timezone.utc) if started_raw else None

        passes = (
            direction == 2
            and duration >= client["min_call_seconds"]
            and extension in (client["sales_extensions"] or [])
        )
        if not passes:
            new_status = "skipped"
        elif recording_id:
            new_status = "new"
        else:
            new_status = "awaiting_record"
        if new_status != "skipped":
            to_analyze += 1

        row_id = await pool.fetchval(
            """
            INSERT INTO calls (client_id, external_id, direction, extension, client_number,
                                duration_seconds, recording_id, status, raw_summary, call_started_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10)
            ON CONFLICT (client_id, external_id) DO UPDATE SET
                direction = EXCLUDED.direction,
                extension = EXCLUDED.extension,
                client_number = EXCLUDED.client_number,
                duration_seconds = EXCLUDED.duration_seconds,
                recording_id = COALESCE(EXCLUDED.recording_id, calls.recording_id),
                raw_summary = EXCLUDED.raw_summary,
                call_started_at = COALESCE(EXCLUDED.call_started_at, calls.call_started_at),
                status = CASE
                    WHEN calls.status IN ('processing','analyzed','delivered','no_recording','failed')
                    THEN calls.status ELSE EXCLUDED.status
                END,
                updated_at = now()
            RETURNING id
            """,
            client["id"], entry_id, direction, extension, client_number,
            duration, recording_id, new_status, json.dumps(c, ensure_ascii=False), call_started_at,
        )

        if new_status == "new":
            await _enqueue_analyze_call(pool, client["id"], row_id, entry_id)

    await pool.execute("UPDATE clients SET last_call_synced_at=$2 WHERE id=$1", client["id"], now_utc)
    if calls:
        log.info("client id=%s опрос Mango: звонков в окне=%s, в разбор=%s", client["id"], len(calls), to_analyze)
    return len(calls), to_analyze


async def _send_heartbeat(pool: asyncpg.Pool, client: asyncpg.Record, found: int, to_analyze: int,
                           error: str | None) -> None:
    """Подтверждение РОПу, что опрос реально прошёл — без этого поломка опроса
    (как было с часовым поясом у Claude-стороны) молчит сутками, ничего не
    показывая в чате. found — все записи от Mango за окно (включая входящие,
    короткие и не по нужным добавочным); to_analyze — сколько из них реально
    пойдут в разбор."""
    head_id = await _head_chat_id(pool, client["id"])
    if not head_id:
        return
    tz = ZoneInfo(client["timezone"])
    stamp = datetime.now(tz).strftime("%d.%m %H:%M")
    if error:
        text = f"⚠️ Astra: ошибка опроса Mango в {stamp} — {esc(error)[:300]}. Попробую в следующий раз."
    elif found == 0:
        text = f"🔄 Astra проверила Mango в {stamp} — новых записей нет."
    else:
        text = (f"🔄 Astra проверила Mango в {stamp} — найдено записей: {found}, "
                f"из них пойдёт в разбор: {to_analyze} (остальное — входящие/короткие/не те добавочные).")
    try:
        await bot.send_message(head_id, text)
    except Exception:
        log.exception("не удалось отправить heartbeat РОПу")


async def poll_all_clients(pool: asyncpg.Pool, next_poll_at: dict[int, float]) -> None:
    now = time.monotonic()
    clients = await pool.fetch("SELECT * FROM clients")
    for client in clients:
        if now < next_poll_at.get(client["id"], 0):
            continue
        next_poll_at[client["id"]] = now + client["mango_poll_interval_sec"]
        try:
            found, to_analyze = await poll_client_calls(pool, client)
            await _send_heartbeat(pool, client, found, to_analyze, None)
        except Exception as exc:
            log.exception("ошибка опроса Mango, client id=%s", client["id"])
            await _send_heartbeat(pool, client, 0, 0, str(exc))


# --------------------------------------------------------------- обслуживание

async def sweep_timeouts(pool: asyncpg.Pool) -> None:
    result = await pool.execute(
        """
        UPDATE calls c SET status = 'no_recording', updated_at = now()
        FROM clients cl
        WHERE c.client_id = cl.id
          AND c.status = 'awaiting_record'
          AND c.created_at < now() - (cl.recording_wait_timeout_min || ' minutes')::interval
        """
    )
    if result != "UPDATE 0":
        log.info("таймаут ожидания записи: %s", result)


# ------------------------------------------------------- дайджесты агента РОПа

# Этап 3: 9:00 три темы на планёрку, понедельник 10:00 сводка за неделю, 1
# число 10:00 отчёт собственнику. Этот процесс только кладёт задачи в общую
# очередь (tasks) — сам вопрос агенту РОПа и отправка ответа в Telegram
# делает обработчик handlers/rop_digest.py в queue_runner. Планировщик и
# исполнитель не общаются напрямую, только через таблицу — тот же принцип,
# что и у analyze_call.

ROP_DIGEST_GRACE_HOURS = 2


async def maybe_enqueue_rop_digest_for_client(pool: asyncpg.Pool, client: asyncpg.Record) -> None:
    tz = ZoneInfo(client["timezone"])
    now_local = datetime.now(tz)
    if now_local.weekday() >= 5:
        return

    candidates = [("morning", dtime(9, 0))]
    if now_local.weekday() == 0:
        candidates.append(("weekly", dtime(10, 0)))
    if now_local.day == 1:
        candidates.append(("monthly", dtime(10, 0)))

    for kind, at in candidates:
        target = datetime.combine(now_local.date(), at, tzinfo=tz)
        if not (target <= now_local < target + timedelta(hours=ROP_DIGEST_GRACE_HOURS)):
            continue
        state = await pool.fetchrow(
            "SELECT * FROM rop_digest_state WHERE client_id=$1 AND kind=$2", client["id"], kind
        )
        if state and state["last_sent_date"] == now_local.date():
            continue
        await pool.execute(
            """
            INSERT INTO tasks (type, client_id, input, dedup_key)
            VALUES ($1, $2, $3::jsonb, $4)
            ON CONFLICT (type, dedup_key) DO NOTHING
            """,
            f"rop_digest_{kind}", client["id"], json.dumps({"client_id": client["id"]}),
            f"{kind}:{client['id']}:{now_local.date().isoformat()}",
        )
        await pool.execute(
            """
            INSERT INTO rop_digest_state (client_id, kind, last_sent_date) VALUES ($1, $2, $3)
            ON CONFLICT (client_id, kind) DO UPDATE SET last_sent_date = EXCLUDED.last_sent_date
            """,
            client["id"], kind, now_local.date(),
        )
        log.info("client id=%s поставлена задача rop_digest_%s", client["id"], kind)


async def maybe_enqueue_rop_digests(pool: asyncpg.Pool) -> None:
    clients = await pool.fetch("SELECT * FROM clients WHERE processing_enabled = true")
    for client in clients:
        try:
            await maybe_enqueue_rop_digest_for_client(pool, client)
        except Exception:
            log.exception("ошибка планировщика дайджестов РОПа, client id=%s", client["id"])


# -------------------------------------------------------------------- дайджест

LEVELS = ("✅", "❌", "❌❌")

CRITERION_BUCKET = {
    "call_reason": "скрипт", "gatekeeper_passed": "скрипт",
    "implication_questions": "менеджер", "problem_over_situational": "менеджер", "explicit_need": "менеджер",
    "decision_influence": "менеджер", "brush_off_handled": "менеджер", "sold_meeting": "менеджер",
    "concrete_commitment": "менеджер", "no_early_pitch": "менеджер", "insight": "менеджер", "talk_share": "менеджер",
}
BUCKET_HINT = {
    "менеджер": "похоже, дело в технике самого разговора — стоит потренировать этот момент отдельно",
    "скрипт": "похоже, дело в самой структуре звонка (повод, выход на нужного человека) — стоит пересмотреть сценарий",
}


def _level_counts(rows) -> dict[str, int]:
    counts = {lvl: 0 for lvl in LEVELS}
    for r in rows:
        if r["level"] in counts:
            counts[r["level"]] += 1
    return counts


def _one_liner(short_report_json) -> str | None:
    if not short_report_json:
        return None
    weak = (json.loads(short_report_json).get("weak") or [None])[0]
    if not weak or not weak.get("text"):
        return None
    prefix = f"{weak['time']} " if weak.get("time") else ""
    return f"{prefix}{esc(weak['text'])}"


async def build_manager_digest(pool: asyncpg.Pool, client: asyncpg.Record, extension: str, name: str,
                                today_start: datetime, today_end: datetime) -> str | None:
    rows = await pool.fetch(
        """SELECT a.level, a.short_report, a.immediate_sent_manager FROM astra_analysis a
           JOIN calls c ON c.id = a.call_id
           WHERE c.client_id=$1 AND c.extension=$2 AND a.status='analyzed'
             AND a.updated_at >= $3 AND a.updated_at < $4
           ORDER BY a.updated_at""",
        client["id"], extension, today_start, today_end,
    )
    if not rows:
        return None
    counts = _level_counts(rows)
    out = [
        f"<b>📊 Дайджест за день</b> · {esc(name)}",
        f"Звонков: {len(rows)} — ✅ {counts['✅']} · ❌ {counts['❌']} · ❌❌ {counts['❌❌']}",
    ]
    not_yet_sent = [r for r in rows if not r["immediate_sent_manager"] and r["level"] in ("❌", "❌❌")]
    if not_yet_sent:
        worst = min(not_yet_sent, key=lambda r: 0 if r["level"] == "❌❌" else 1)
        line = _one_liner(worst["short_report"])
        if line:
            out += ["", "<b>Худший звонок сегодня</b>", line]
    return "\n".join(out)


async def build_head_digest(pool: asyncpg.Pool, client: asyncpg.Record,
                             today_start: datetime, today_end: datetime) -> str | None:
    rows = await pool.fetch(
        """SELECT c.extension, a.level, a.short_report, a.analysis FROM astra_analysis a
           JOIN calls c ON c.id = a.call_id
           WHERE c.client_id=$1 AND a.status='analyzed'
             AND a.updated_at >= $2 AND a.updated_at < $3""",
        client["id"], today_start, today_end,
    )
    if not rows:
        return None
    employees = await pool.fetch(
        "SELECT extension, full_name, telegram_user_id FROM employees WHERE client_id=$1 AND role='manager'",
        client["id"],
    )
    names = {e["extension"]: e["full_name"] for e in employees}
    linked = {e["extension"] for e in employees if e["telegram_user_id"]}

    per_manager: dict[str, list] = {}
    fail_count: dict[str, int] = {}
    total_count: dict[str, int] = {}
    for r in rows:
        per_manager.setdefault(r["extension"], []).append(r)
        if not r["analysis"]:
            continue
        for row in (json.loads(r["analysis"]).get("rows") or []):
            if not row.get("applicable"):
                continue
            total_count[row["title"]] = total_count.get(row["title"], 0) + 1
            if not row.get("passed"):
                fail_count[row["title"]] = fail_count.get(row["title"], 0) + 1

    def _share(ext_rows):
        c = _level_counts(ext_rows)
        n = len(ext_rows)
        return c["✅"] / n, c["❌❌"] / n

    ranking = sorted(per_manager.items(), key=lambda kv: (-_share(kv[1])[0], _share(kv[1])[1]))
    out = ["<b>📊 Дайджест РОПу за день</b>", f"Всего звонков разобрано: {len(rows)}"]
    for ext, ext_rows in ranking:
        c = _level_counts(ext_rows)
        out.append(f"• {esc(names.get(ext) or ext)} — ✅ {c['✅']} · ❌ {c['❌']} · ❌❌ {c['❌❌']}")
        worst = next((r for r in ext_rows if r["level"] in ("❌", "❌❌")), None)
        line = _one_liner(worst["short_report"]) if worst else None
        if line:
            out.append(f"   Худший звонок: {line}")
        if ext not in linked:
            out.append(f"   ⚠️ Не подключён к боту — /invite {esc(ext)}")

    if fail_count:
        title, n = max(fail_count.items(), key=lambda kv: kv[1])
        key = next((k for k, _w, t in CRITERIA if t == title), None)
        bucket = CRITERION_BUCKET.get(key)
        hint = BUCKET_HINT.get(bucket, "стоит присмотреться к этому месту в звонках отдельно")
        out += ["", "<b>В ЧЁМ МОЖЕТ БЫТЬ ДЕЛО</b>",
                f"Чаще всего проваливается «{esc(title)}» ({n} из {total_count[title]} звонков, где критерий "
                f"был применим) — {hint}. Возможно, дело и в другом, это не точный вывод."]

    return "\n".join(out)


async def maybe_send_digest_for_client(pool: asyncpg.Pool, client: asyncpg.Record) -> None:
    tz = ZoneInfo(client["timezone"])
    now_local = datetime.now(tz)
    if now_local.weekday() >= 5:
        return

    digest_dt = datetime.combine(now_local.date(), client["digest_time"], tzinfo=tz)
    if not (digest_dt <= now_local < digest_dt + timedelta(hours=DIGEST_GRACE_HOURS)):
        return

    state = await pool.fetchrow("SELECT * FROM astra_digest_state WHERE client_id=$1", client["id"])
    if state and state["last_digest_sent_date"] == now_local.date():
        return

    today_start, today_end = _day_bounds(client)
    pending = await pool.fetch(
        """SELECT c.extension FROM astra_analysis a JOIN calls c ON c.id = a.call_id
           WHERE c.client_id=$1 AND a.status='analyzed' AND a.digest_included=false
             AND a.updated_at >= $2 AND a.updated_at < $3""",
        client["id"], today_start, today_end,
    )
    if not pending:
        return

    employees = await pool.fetch("SELECT * FROM employees WHERE client_id=$1", client["id"])
    by_ext = {e["extension"]: e for e in employees if e["role"] == "manager"}
    head = next((e for e in employees if e["role"] == "head"), None)

    for ext in sorted({p["extension"] for p in pending if p["extension"]}):
        emp = by_ext.get(ext)
        if not emp or not emp["telegram_user_id"]:
            continue
        text = await build_manager_digest(pool, client, ext, emp["full_name"] or ext, today_start, today_end)
        if text:
            try:
                await send_long(bot, emp["telegram_user_id"], text)
            except Exception:
                log.exception("не удалось отправить дайджест менеджеру доб.=%s", ext)

    if head and head["telegram_user_id"]:
        text = await build_head_digest(pool, client, today_start, today_end)
        if text:
            try:
                await send_long(bot, head["telegram_user_id"], text)
            except Exception:
                log.exception("не удалось отправить дайджест РОПу")

    await pool.execute(
        """UPDATE astra_analysis a SET digest_included=true, updated_at=now()
           FROM calls c WHERE c.id = a.call_id AND c.client_id=$1 AND a.status='analyzed'
             AND a.digest_included=false AND a.updated_at >= $2 AND a.updated_at < $3""",
        client["id"], today_start, today_end,
    )
    await pool.execute(
        """INSERT INTO astra_digest_state (client_id, last_digest_sent_date) VALUES ($1, $2)
           ON CONFLICT (client_id) DO UPDATE SET last_digest_sent_date = EXCLUDED.last_digest_sent_date""",
        client["id"], now_local.date(),
    )
    log.info("client id=%s дайджест за день отправлен, звонков=%s", client["id"], len(pending))


async def maybe_send_digests(pool: asyncpg.Pool) -> None:
    clients = await pool.fetch("SELECT * FROM clients WHERE processing_enabled = true")
    for client in clients:
        try:
            await maybe_send_digest_for_client(pool, client)
        except Exception:
            log.exception("ошибка дайджеста, client id=%s", client["id"])


# ----------------------------------------------------------------------- цикл

async def main() -> None:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=5)
    log.info("astra worker started (poll + enqueue only; разбор — в queue_runner)")
    last_sweep = 0.0
    next_poll_at: dict[int, float] = {}
    try:
        while True:
            now = time.monotonic()
            await poll_all_clients(pool, next_poll_at)
            if now - last_sweep > SWEEP_INTERVAL_SEC:
                await sweep_timeouts(pool)
                await maybe_send_digests(pool)
                await maybe_enqueue_rop_digests(pool)
                last_sweep = now
            await asyncio.sleep(POLL_INTERVAL_SEC)
    finally:
        await pool.close()
        await bot.session.close()
        await mango_client.close()


if __name__ == "__main__":
    asyncio.run(main())
