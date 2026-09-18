"""
Опрос Mango + доставка дайджестов для Astra-конвейера. Сам разбор звонка
(транскрибация, оценка, немедленная доставка) больше не выполняется в этом
процессе — с Этапа 1 мультиагентной шины это обработчик очереди
`analyze_call` (handlers/analyze_call.py), который забирает задачи через
queue_runner.py. Здесь остаётся только: опрос Mango и постановка задач в
очередь, sweep таймаутов записи, вечерний дайджест.

Источник звонков — ОПРОС Mango (poll_client_calls), не вебхуки — push-вебхуки
у Mango платные, базовый API (stats/calls/request + result) бесплатный и уже
используется.

Опрос — раз в client.mango_poll_interval_sec (сейчас 300 сек = 5 минут).
Успешный опрос пишется только в лог; в Telegram уходят лишь сообщения об
ошибках опроса, не чаще раза в час (см. историю с часовым поясом — опрос
молчал сутками, ничего не показывая как сломанное, поэтому совсем без сигнала
об ошибке оставлять нельзя).
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
from tools import owner_chat_ids, report_chat_ids

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
    """Возвращает (найдено всего, пойдёт в разбор) — для лога."""
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


# Рутинный heartbeat («проверила Mango, новых записей нет») убран по просьбе
# пользователя — при опросе раз в 5 минут это 12 сообщений в час ни о чём.
# Сообщения об ОШИБКАХ опроса оставлены: именно ради них heartbeat и заводился
# после истории с часовым поясом, когда опрос молча простоял сутки. Успешный
# опрос виден в логе (journalctl -u callbot-astra-worker), а не в чате.
ERROR_NOTICE_COOLDOWN_SEC = 3600
_last_error_notice: dict[int, float] = {}


async def _send_poll_error(pool: asyncpg.Pool, client: asyncpg.Record, error: str) -> None:
    """Уходит только владельцу системы: сломанный опрос Mango — это про
    работу системы, а не про работу отдела, и руководителю с этим делать
    нечего. Не чаще раза в час на клиента: при опросе каждые 5 минут устойчивая
    поломка иначе завалила бы чат одинаковыми сообщениями."""
    now = time.monotonic()
    if now - _last_error_notice.get(client["id"], 0) < ERROR_NOTICE_COOLDOWN_SEC:
        return
    # Если владелец не привязан к боту — лучше сказать хоть кому-то, чем
    # промолчать: именно из-за молчания опрос однажды простоял сутки.
    chat_ids = await owner_chat_ids(pool, client["id"]) or await report_chat_ids(pool, client["id"])
    if not chat_ids:
        return
    _last_error_notice[client["id"]] = now
    tz = ZoneInfo(client["timezone"])
    stamp = datetime.now(tz).strftime("%d.%m %H:%M")
    for chat_id in chat_ids:
        try:
            await bot.send_message(
                chat_id,
                f"⚠️ Astra: ошибка опроса Mango в {stamp} — {esc(error)[:300]}. "
                f"Продолжаю попытки, следующее сообщение об этой проблеме — не раньше чем через час.",
                parse_mode="HTML",  # текст ошибки прогнан через esc()
            )
        except Exception:
            log.exception("не удалось отправить сообщение об ошибке опроса, chat_id=%s", chat_id)


async def poll_all_clients(pool: asyncpg.Pool, next_poll_at: dict[int, float]) -> None:
    now = time.monotonic()
    clients = await pool.fetch("SELECT * FROM clients")
    for client in clients:
        if now < next_poll_at.get(client["id"], 0):
            continue
        next_poll_at[client["id"]] = now + client["mango_poll_interval_sec"]
        try:
            found, to_analyze = await poll_client_calls(pool, client)
            log.info("client id=%s опрос завершён: найдено=%s, в разбор=%s", client["id"], found, to_analyze)
            _last_error_notice.pop(client["id"], None)
        except Exception as exc:
            log.exception("ошибка опроса Mango, client id=%s", client["id"])
            await _send_poll_error(pool, client, str(exc))


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


OVERSIGHT_TZ = os.getenv("OVERSIGHT_TZ", "Europe/Moscow")
OVERSIGHT_AT = dtime(22, 0)


async def maybe_enqueue_oversight_report(pool: asyncpg.Pool) -> None:
    """Отчёт надзора системный, а не по клиенту — отсюда единственная строка
    состояния и собственная таймзона, не клиентская."""
    tz = ZoneInfo(OVERSIGHT_TZ)
    now_local = datetime.now(tz)
    target = datetime.combine(now_local.date(), OVERSIGHT_AT, tzinfo=tz)
    if not (target <= now_local < target + timedelta(hours=ROP_DIGEST_GRACE_HOURS)):
        return

    state = await pool.fetchrow("SELECT * FROM oversight_state WHERE id=1")
    if state and state["last_sent_date"] == now_local.date():
        return

    await pool.execute(
        """
        INSERT INTO tasks (type, input, dedup_key)
        VALUES ('oversight_report', '{}'::jsonb, $1)
        ON CONFLICT (type, dedup_key) DO NOTHING
        """,
        f"oversight:{now_local.date().isoformat()}",
    )
    await pool.execute(
        """
        INSERT INTO oversight_state (id, last_sent_date) VALUES (1, $1)
        ON CONFLICT (id) DO UPDATE SET last_sent_date = EXCLUDED.last_sent_date
        """,
        now_local.date(),
    )
    log.info("поставлена задача oversight_report за %s", now_local.date())


async def maybe_enqueue_rop_digests(pool: asyncpg.Pool) -> None:
    clients = await pool.fetch("SELECT * FROM clients WHERE processing_enabled = true")
    for client in clients:
        try:
            await maybe_enqueue_rop_digest_for_client(pool, client)
        except Exception:
            log.exception("ошибка планировщика дайджестов РОПа, client id=%s", client["id"])


# -------------------------------------------------------------------- дайджест

LEVELS = ("✅", "⚠️", "❌")

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


def _unrated(rows) -> int:
    """Звонки без вердикта: модель пометила их как оборванные — слишком
    короткие, рваные или вообще автоответчик («Вас приветствует компания…»),
    судить о работе менеджера там не по чему. Их обязательно показывать
    отдельной цифрой: раньше дайджест писал «разобрано 6» и рисовал нули по
    уровням, и выглядело это как «шесть звонков на ноль», хотя оценивать было
    нечего."""
    return sum(1 for r in rows if not r["level"])


def _fmt_levels(rows) -> str:
    c = _level_counts(rows)
    out = f"✅ {c['✅']} · ⚠️ {c['⚠️']} · ❌ {c['❌']}"
    unrated = _unrated(rows)
    if unrated:
        out += f" · без оценки {unrated}"
    return out


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
    # Звонки без вердикта (оборванные, автоответчики) в дайджест менеджеру не
    # входят вообще — ни в счёт, ни отдельной строкой: ему важна своя работа, а
    # там работы не было. Руководителю они по-прежнему видны цифрой «без
    # оценки»: ему нужно понимать, сколько времени отдел потратил впустую.
    rows = [r for r in rows if r["level"]]
    if not rows:
        return None
    out = [
        f"<b>📊 Дайджест за день</b> · {esc(name)}",
        f"Звонков: {len(rows)} — {_fmt_levels(rows)}",
    ]
    not_yet_sent = [r for r in rows if not r["immediate_sent_manager"] and r["level"] in ("⚠️", "❌")]
    if not_yet_sent:
        worst = min(not_yet_sent, key=lambda r: 0 if r["level"] == "❌" else 1)
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
        return c["✅"] / n, c["❌"] / n

    ranking = sorted(per_manager.items(), key=lambda kv: (-_share(kv[1])[0], _share(kv[1])[1]))
    unrated = _unrated(rows)
    header = f"Всего звонков разобрано: {len(rows)}"
    if unrated:
        header += (f", из них {unrated} без оценки — оборванные или автоответчик, "
                   f"судить там не по чему")
    out = ["<b>📊 Дайджест РОПу за день</b>", header]
    for ext, ext_rows in ranking:
        c = _level_counts(ext_rows)
        out.append(f"• {esc(names.get(ext) or ext)} — {_fmt_levels(ext_rows)}")
        worst = next((r for r in ext_rows if r["level"] in ("⚠️", "❌")), None)
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
    heads = await report_chat_ids(pool, client["id"])

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

    # Вечерний отчёт руководителю собирает агент (handlers/rop_digest.py):
    # точные числа считает код, а недочёты и что с ними делать — рассуждение.
    # Здесь только ставим задачу в очередь: планировщик и исполнитель не
    # общаются напрямую, только через таблицу tasks.
    if heads:
        await pool.execute(
            """
            INSERT INTO tasks (type, client_id, input, dedup_key)
            VALUES ('rop_digest_evening', $1, $2::jsonb, $3)
            ON CONFLICT (type, dedup_key) DO NOTHING
            """,
            client["id"], json.dumps({"client_id": client["id"]}),
            f"evening:{client['id']}:{now_local.date().isoformat()}",
        )
        log.info("client id=%s поставлена задача rop_digest_evening", client["id"])

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
                await maybe_enqueue_oversight_report(pool)
                last_sweep = now
            await asyncio.sleep(POLL_INTERVAL_SEC)
    finally:
        await pool.close()
        await bot.session.close()
        await mango_client.close()


if __name__ == "__main__":
    asyncio.run(main())
