"""
Полностью самостоятельный конвейер Astra — не зависит от /opt/callbot
(Claude-бот отключён). Свой опрос Mango, своя транскрибация через Deepgram,
свой анализ и доставка. Отдельный процесс, systemd-юнит callbot-astra-worker.

Источник звонков — ОПРОС Mango (poll_client_calls), не вебхуки — та же
причина, что и была у Claude-стороны: push-вебхуки платные, базовый API
(stats/calls/request + result) бесплатный и уже используется.

Опрос — раз в client.mango_poll_interval_sec (сейчас выставлено 900 сек = 15
минут, по явной просьбе — специально реже, чем было у Claude, поскольку
здесь этот интервал ничего не экономит по деньгам, только частоту опроса).
После каждого цикла опроса — heartbeat-сообщение РОПу в Telegram: воркер
реально проверяет Mango, а не тихо стоит (см. историю с часовым поясом —
опрос молчал сутками, ничего не показывая в логах как сломанное).

Очередь на обработку — SELECT ... FOR UPDATE SKIP LOCKED в Postgres, как и
было у Claude-воркера. calls — общая таблица схемы (ею раньше владел
Claude-воркер, теперь пишет сюда только этот процесс); astra_analysis —
результаты именно Astra-анализа (score/level/short_report/detailed_report),
отдельно от сырых данных звонка.
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
from analysis import CRITERIA, score_call, short_report_call
from crypto_util import decrypt
from deepgram_client import close as close_deepgram
from deepgram_client import transcribe_bytes
from reports import detail_button, esc, fmt_call_time, render_short, send_long, send_short_report

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("callbot-astra-worker")

bot = Bot(token=os.environ["BOT_TOKEN"])

POLL_INTERVAL_SEC = 5
SWEEP_INTERVAL_SEC = 60
BUDGET_RECHECK_MIN = 30
DIGEST_GRACE_HOURS = 2

POLL_OVERLAP_MIN = 3
INITIAL_LOOKBACK_MIN = 60
MAX_LOOKBACK_DAYS = 25
STATS_RESULT_MAX_ATTEMPTS = 10
STATS_RESULT_RETRY_SEC = 2

BACKOFF_MINUTES = [2, 10, 30, 60, 120]
MAX_ATTEMPTS = len(BACKOFF_MINUTES)

DG_PRICE_PER_MIN_USD = 0.0043  # nova-3, предзаписанное аудио — только для лога, не бюджетный гейт

# Пер-клиентский дневной потолок расхода в "кредит-единицах" Astra.
DAILY_UNIT_LIMIT = float(os.getenv("MAX_COST_PER_CHAT_UNITS", "2000000"))


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

        await pool.execute(
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
            """,
            client["id"], entry_id, direction, extension, client_number,
            duration, recording_id, new_status, json.dumps(c, ensure_ascii=False), call_started_at,
        )

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
    пойдут в разбор. Раньше heartbeat показывал только found — выглядело как
    «звонок пропал», хотя он был корректно отфильтрован (не исходящий /
    слишком короткий / не тот добавочный)."""
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


async def reset_orphaned(pool: asyncpg.Pool) -> None:
    result = await pool.execute("UPDATE calls SET status='new', updated_at=now() WHERE status='processing'")
    if result != "UPDATE 0":
        log.warning("восстановлены зависшие processing-строки после перезапуска: %s", result)
    result = await pool.execute("UPDATE astra_analysis SET status='new' WHERE status='processing'")
    if result != "UPDATE 0":
        log.warning("восстановлены зависшие processing-строки astra_analysis: %s", result)


# -------------------------------------------------------------------- очередь

async def claim_next(pool: asyncpg.Pool) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT c.* FROM calls c
                JOIN clients cl ON cl.id = c.client_id
                WHERE c.status = 'new'
                  AND cl.processing_enabled = true
                  AND (c.next_attempt_at IS NULL OR c.next_attempt_at <= now())
                ORDER BY c.created_at
                FOR UPDATE OF c SKIP LOCKED
                LIMIT 1
                """
            )
            if row:
                await conn.execute("UPDATE calls SET status='processing', updated_at=now() WHERE id=$1", row["id"])
                await conn.execute(
                    "INSERT INTO astra_analysis (call_id, status) VALUES ($1,'processing') "
                    "ON CONFLICT (call_id) DO UPDATE SET status='processing'",
                    row["id"],
                )
    return row


async def over_daily_limit(pool: asyncpg.Pool, client: asyncpg.Record) -> bool:
    spent = await pool.fetchval(
        "SELECT spent_units FROM astra_daily_spend WHERE client_id=$1 AND day=$2",
        client["id"], datetime.now(ZoneInfo(client["timezone"])).date(),
    )
    return float(spent or 0) >= DAILY_UNIT_LIMIT


async def add_spend(pool: asyncpg.Pool, client: asyncpg.Record, units: float) -> None:
    await pool.execute(
        """
        INSERT INTO astra_daily_spend (client_id, day, spent_units) VALUES ($1, $2, $3)
        ON CONFLICT (client_id, day) DO UPDATE SET spent_units = astra_daily_spend.spent_units + EXCLUDED.spent_units
        """,
        client["id"], datetime.now(ZoneInfo(client["timezone"])).date(), units,
    )


async def fail_or_retry(pool: asyncpg.Pool, call: asyncpg.Record, exc: Exception) -> None:
    attempts = call["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        log.error("call id=%s окончательно провалена после %s попыток: %s", call["id"], attempts, exc)
        await pool.execute("UPDATE calls SET status='failed', attempts=$2, updated_at=now() WHERE id=$1",
                            call["id"], attempts)
        await pool.execute("UPDATE astra_analysis SET status='failed', error=$2 WHERE call_id=$1",
                            call["id"], str(exc)[:2000])
        return
    delay = timedelta(minutes=BACKOFF_MINUTES[attempts - 1])
    log.warning("call id=%s попытка %s не удалась (%s), повтор через %s", call["id"], attempts, exc, delay)
    await pool.execute(
        "UPDATE calls SET status='new', attempts=$2, next_attempt_at=now()+$3, updated_at=now() WHERE id=$1",
        call["id"], attempts, delay,
    )
    await pool.execute("UPDATE astra_analysis SET status='new' WHERE call_id=$1", call["id"])


# ------------------------------------------------------------------ доставка

async def manager_immediate_count_today(pool: asyncpg.Pool, client: asyncpg.Record, extension: str) -> int:
    start, end = _day_bounds(client)
    return await pool.fetchval(
        """SELECT count(*) FROM astra_analysis a JOIN calls c ON c.id = a.call_id
           WHERE c.client_id=$1 AND c.extension=$2 AND a.immediate_sent_manager=true
             AND a.updated_at >= $3 AND a.updated_at < $4""",
        client["id"], extension, start, end,
    )


async def head_immediate_count_today(pool: asyncpg.Pool, client: asyncpg.Record) -> int:
    start, end = _day_bounds(client)
    return await pool.fetchval(
        """SELECT count(*) FROM astra_analysis a JOIN calls c ON c.id = a.call_id
           WHERE c.client_id=$1 AND a.immediate_sent_head=true
             AND a.updated_at >= $2 AND a.updated_at < $3""",
        client["id"], start, end,
    )


async def deliver_immediate(pool: asyncpg.Pool, client: asyncpg.Record, call: asyncpg.Record,
                             short_report: dict, level: str | None) -> None:
    if level != "❌❌":
        return

    call_time = fmt_call_time(call["call_started_at"], client["timezone"])

    manager = await pool.fetchrow(
        "SELECT * FROM employees WHERE client_id=$1 AND extension=$2 AND role='manager'",
        client["id"], call["extension"],
    )

    if manager and manager["telegram_user_id"]:
        if await manager_immediate_count_today(pool, client, call["extension"]) < client["max_immediate_per_manager"]:
            try:
                text = render_short(short_report, level, call["duration_seconds"] or 0, call_time=call_time)
                await send_short_report(bot, manager["telegram_user_id"], text, detail_button(call["id"], source="astra"))
                await pool.execute(
                    "UPDATE astra_analysis SET immediate_sent_manager=true, updated_at=now() WHERE call_id=$1", call["id"]
                )
            except Exception:
                log.exception("не удалось отправить менеджеру, call id=%s", call["id"])

    head = await pool.fetchrow("SELECT * FROM employees WHERE client_id=$1 AND role='head'", client["id"])
    if head and head["telegram_user_id"]:
        if await head_immediate_count_today(pool, client) < client["max_immediate_per_head"]:
            manager_name = (manager["full_name"] if manager else None) or f"доб. {call['extension']}"
            try:
                text = render_short(short_report, level, call["duration_seconds"] or 0, manager_name, call_time=call_time)
                if not (manager and manager["telegram_user_id"]):
                    text += (f"\n\n⚠️ Менеджер (доб. {esc(call['extension'])}) не подключён к боту — личный "
                             f"разбор не отправлен.")
                await send_short_report(bot, head["telegram_user_id"], text, detail_button(call["id"], source="astra"))
                await pool.execute(
                    "UPDATE astra_analysis SET immediate_sent_head=true, updated_at=now() WHERE call_id=$1", call["id"]
                )
            except Exception:
                log.exception("не удалось отправить РОПу, call id=%s", call["id"])


# ------------------------------------------------------------------ обработка

async def process_call(pool: asyncpg.Pool, call: asyncpg.Record) -> None:
    client = await pool.fetchrow("SELECT * FROM clients WHERE id=$1", call["client_id"])

    if await over_daily_limit(pool, client):
        log.warning("client id=%s превысил дневной лимит, call id=%s отложен", client["id"], call["id"])
        await pool.execute(
            "UPDATE calls SET status='new', next_attempt_at=now()+$2, updated_at=now() WHERE id=$1",
            call["id"], timedelta(minutes=BUDGET_RECHECK_MIN),
        )
        return

    try:
        vpbx_api_key = decrypt(client["vpbx_api_key_enc"])
        vpbx_api_salt = decrypt(client["vpbx_api_salt_enc"])

        audio = await mango_client.fetch_recording(vpbx_api_key, vpbx_api_salt, call["recording_id"])
        transcript, dg_duration = await transcribe_bytes(audio)
        del audio

        scores, score, level, rows, cost = await score_call(transcript)
        short_report, short_cost = await short_report_call(transcript, scores, rows, level)
        cost += short_cost
        analysis = {**scores, "rows": rows}

        await add_spend(pool, client, cost)
        log.info("Deepgram (справочно, не в бюджете Astra): $%.4f", dg_duration / 60 * DG_PRICE_PER_MIN_USD)

    except Exception as exc:
        log.exception("ошибка обработки call id=%s", call["id"])
        await fail_or_retry(pool, call, exc)
        return

    await pool.execute(
        "UPDATE calls SET transcript=$2, status='analyzed', updated_at=now() WHERE id=$1",
        call["id"], transcript,
    )
    await pool.execute(
        """
        UPDATE astra_analysis SET
            status='analyzed', analysis=$2::jsonb, score=$3, level=$4, short_report=$5::jsonb,
            cost_units=$6, updated_at=now()
        WHERE call_id=$1
        """,
        call["id"], json.dumps(analysis, ensure_ascii=False), score, level,
        json.dumps(short_report, ensure_ascii=False), cost,
    )
    log.info("call id=%s разобран: уровень=%s стоимость=%.0f ед.", call["id"], level, cost)

    try:
        await deliver_immediate(pool, client, call, short_report, level)
    except Exception:
        log.exception("ошибка немедленной доставки, call id=%s", call["id"])


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
    await reset_orphaned(pool)
    log.info("astra worker started (standalone)")
    last_sweep = 0.0
    next_poll_at: dict[int, float] = {}
    try:
        while True:
            now = time.monotonic()
            await poll_all_clients(pool, next_poll_at)
            if now - last_sweep > SWEEP_INTERVAL_SEC:
                await sweep_timeouts(pool)
                await maybe_send_digests(pool)
                last_sweep = now
            call = await claim_next(pool)
            if call is None:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue
            await process_call(pool, call)
    finally:
        await pool.close()
        await bot.session.close()
        await mango_client.close()
        await close_deepgram()


if __name__ == "__main__":
    asyncio.run(main())
