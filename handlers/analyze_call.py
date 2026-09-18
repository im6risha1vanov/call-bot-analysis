"""
Обработчик типа задачи `analyze_call` — регистрируется в queue_runner при
импорте модуля. Логика перенесена из прежнего astra_worker.process_call()
без изменений по существу: раннер отвечает за очередь и повторы, обработчик —
только за сам разбор звонка и доставку. Задачи сюда кладёт
astra_worker.poll_client_calls() вместо прежнего прямого вызова
claim_next()/process_call() на таблице calls.

Число запросов к модели, критерии и их веса не менялись — см. analysis.py.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot

import mango_client
from analysis import score_call, short_report_call
from crypto_util import decrypt
from deepgram_client import transcribe_bytes
from queue_runner import RetryLater, on_startup, register
from reports import detail_button, fmt_call_time, render_short, send_short_report
from tools import report_chat_ids

log = logging.getLogger("callbot-astra-worker.analyze_call")

BUDGET_RECHECK_MIN = 30
DG_PRICE_PER_MIN_USD = 0.0043  # nova-3, предзаписанное аудио — только для лога
DAILY_UNIT_LIMIT = float(os.getenv("MAX_COST_PER_CHAT_UNITS", "2000000"))

_bot = Bot(token=os.environ["BOT_TOKEN"])


async def _over_daily_limit(pool: asyncpg.Pool, client: asyncpg.Record) -> bool:
    spent = await pool.fetchval(
        "SELECT spent_units FROM astra_daily_spend WHERE client_id=$1 AND day=$2",
        client["id"], datetime.now(ZoneInfo(client["timezone"])).date(),
    )
    return float(spent or 0) >= DAILY_UNIT_LIMIT


async def _add_spend(pool: asyncpg.Pool, client: asyncpg.Record, units: float) -> None:
    await pool.execute(
        """
        INSERT INTO astra_daily_spend (client_id, day, spent_units) VALUES ($1, $2, $3)
        ON CONFLICT (client_id, day) DO UPDATE SET spent_units = astra_daily_spend.spent_units + EXCLUDED.spent_units
        """,
        client["id"], datetime.now(ZoneInfo(client["timezone"])).date(), units,
    )


async def _deliver_immediate(pool: asyncpg.Pool, client: asyncpg.Record, call: asyncpg.Record,
                              short_report: dict, level: str | None) -> None:
    # Руководителю уходит разбор КАЖДОГО звонка, а не только провального:
    # раньше порог вердикта решал, что он увидит, и половина работы отдела была
    # ему не видна.
    #
    # Исключение — звонки без вердикта: модель помечает их оборванными
    # (слишком короткие, рваные или автоответчик вида «Вас приветствует
    # компания…»). Разбирать там нечего, а в чат это летело бы мусором. Их
    # число видно отдельной цифрой «без оценки» в вечернем дайджесте.
    if level is None:
        return

    call_time = fmt_call_time(call["call_started_at"], client["timezone"])

    manager = await pool.fetchrow(
        "SELECT * FROM employees WHERE client_id=$1 AND extension=$2 AND role='manager'",
        client["id"], call["extension"],
    )

    # Менеджеру уходят все его провальные звонки — суточного лимита нет: он был
    # нужен, чтобы не заваливать человека в рабочее время, но пропущенный разбор
    # своего же провала хуже лишнего сообщения. Порог уровня остаётся: ❌ — это
    # то, где надо разбираться, ✅ и ⚠️ он видит в вечернем дайджесте.
    if manager and manager["telegram_user_id"] and level == "❌":
        try:
            text = render_short(short_report, level, call["duration_seconds"] or 0, call_time=call_time,
                                 client_number=call["client_number"])
            await send_short_report(_bot, manager["telegram_user_id"], text, detail_button(call["id"], source="astra"))
            await pool.execute(
                "UPDATE astra_analysis SET immediate_sent_manager=true, updated_at=now() WHERE call_id=$1", call["id"]
            )
        except Exception:
            log.exception("не удалось отправить менеджеру, call id=%s", call["id"])

    # Получателей несколько: все руководители и владелец системы — отправляем
    # каждому, а не «первому, какой попадётся». Отметка immediate_sent_head одна на звонок: она про то,
    # что звонок уже разослан руководству, а не про конкретного человека.
    # Суточного лимита у руководителя больше нет: он был нужен, когда приходили
    # только провалы и важно было не завалить чат. Теперь задача обратная —
    # видеть все звонки за день.
    recipients = await report_chat_ids(pool, client["id"])
    if recipients:
        manager_name = (manager["full_name"] if manager else None) or f"доб. {call['extension']}"
        text = render_short(short_report, level, call["duration_seconds"] or 0, manager_name, call_time=call_time,
                             client_number=call["client_number"])
        if not (manager and manager["telegram_user_id"]):
            text += (f"\n\n⚠️ Менеджер (доб. {call['extension']}) не подключён к боту — личный "
                     f"разбор не отправлен.")
        delivered = False
        for chat_id in recipients:
            try:
                await send_short_report(_bot, chat_id, text, detail_button(call["id"], source="astra"))
                delivered = True
            except Exception:
                log.exception("не удалось отправить руководителю chat_id=%s, call id=%s", chat_id, call["id"])
        if delivered:
            await pool.execute(
                "UPDATE astra_analysis SET immediate_sent_head=true, updated_at=now() WHERE call_id=$1", call["id"]
            )


@on_startup
async def _reset_orphaned(pool: asyncpg.Pool) -> None:
    """Единственное место, отвечающее за статус 'processing' в calls/
    astra_analysis — раньше этим владел astra_worker, теперь владелец этого
    состояния — обработчик analyze_call."""
    result = await pool.execute("UPDATE calls SET status='new', updated_at=now() WHERE status='processing'")
    if result != "UPDATE 0":
        log.warning("восстановлены зависшие processing-строки calls после перезапуска: %s", result)
    result = await pool.execute("UPDATE astra_analysis SET status='new' WHERE status='processing'")
    if result != "UPDATE 0":
        log.warning("восстановлены зависшие processing-строки astra_analysis после перезапуска: %s", result)


@register("analyze_call")
async def analyze_call(pool: asyncpg.Pool, task: asyncpg.Record) -> dict:
    payload = json.loads(task["input"])
    call = await pool.fetchrow("SELECT * FROM calls WHERE id=$1", payload["call_id"])
    if call is None:
        raise RuntimeError(f"calls.id={payload['call_id']} не найден")
    client = await pool.fetchrow("SELECT * FROM clients WHERE id=$1", call["client_id"])

    if await _over_daily_limit(pool, client):
        raise RetryLater(timedelta(minutes=BUDGET_RECHECK_MIN), "дневной лимит клиента исчерпан")

    await pool.execute("UPDATE calls SET status='processing', updated_at=now() WHERE id=$1", call["id"])
    await pool.execute(
        "INSERT INTO astra_analysis (call_id, status) VALUES ($1,'processing') "
        "ON CONFLICT (call_id) DO UPDATE SET status='processing'",
        call["id"],
    )

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

        await _add_spend(pool, client, cost)
        log.info("Deepgram (справочно, не в бюджете Astra): $%.4f", dg_duration / 60 * DG_PRICE_PER_MIN_USD)
    except Exception:
        log.exception("ошибка обработки call id=%s (задача id=%s)", call["id"], task["id"])
        await pool.execute("UPDATE calls SET status='new', updated_at=now() WHERE id=$1", call["id"])
        await pool.execute("UPDATE astra_analysis SET status='new' WHERE call_id=$1", call["id"])
        raise

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
    log.info("call id=%s разобран через очередь: уровень=%s стоимость=%.0f ед.", call["id"], level, cost)

    try:
        await _deliver_immediate(pool, client, call, short_report, level)
    except Exception:
        log.exception("ошибка немедленной доставки, call id=%s", call["id"])

    return {"call_id": call["id"], "level": level, "cost_units": cost}
