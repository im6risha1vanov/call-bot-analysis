"""
Обработчики плановых дайджестов агента РОПа (Этап 3): 9:00 три темы на
планёрку, понедельник 10:00 сводка за неделю, 1 число 10:00 отчёт
собственнику. Задачи кладёт astra_worker.maybe_enqueue_rop_digests() —
агенты не общаются между собой напрямую, только через общую таблицу tasks.

Каждый обработчик просто прогоняет заранее сформулированный вопрос через тот
же rop_agent.answer(), что и живые вопросы в Telegram (единая логика, единая
проверка выборки и единый проверяющий проход).

Нет отдельной роли "владелец бизнеса" в employees (только head|manager) —
месячный отчёт уходит РОПу (head), как и остальные. Если собственник — другой
человек, это отдельная задача (завести роль/telegram_id), не изобретаю её
здесь.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot

import rop_agent
from queue_runner import register
from tools import Actor

log = logging.getLogger("callbot-astra-worker.rop_digest")

_bot = Bot(token=os.environ["BOT_TOKEN"])


async def _head_actor(pool: asyncpg.Pool, client_id: int) -> Actor | None:
    row = await pool.fetchrow(
        "SELECT telegram_user_id FROM employees WHERE client_id=$1 AND role='head'", client_id
    )
    if row is None or not row["telegram_user_id"]:
        return None
    return Actor(telegram_user_id=row["telegram_user_id"], client_id=client_id, role="head", extension=None)


async def _client_tz(pool: asyncpg.Pool, client_id: int) -> str:
    return await pool.fetchval("SELECT timezone FROM clients WHERE id=$1", client_id)


def _period_yesterday(tz_name: str) -> dict:
    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    yesterday = today - timedelta(days=1)
    return {"start": yesterday.isoformat(), "end": today.isoformat()}


def _period_week(tz_name: str, weeks_ago: int) -> dict:
    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    monday_this_week = today - timedelta(days=today.weekday())
    start = monday_this_week - timedelta(weeks=weeks_ago)
    return {"start": start.isoformat(), "end": (start + timedelta(days=7)).isoformat()}


def _period_prev_month(tz_name: str) -> dict:
    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    first_of_this_month = today.replace(day=1)
    last_of_prev = first_of_this_month - timedelta(days=1)
    return {"start": last_of_prev.replace(day=1).isoformat(), "end": first_of_this_month.isoformat()}


@register("rop_digest_morning")
async def rop_digest_morning(pool: asyncpg.Pool, task: asyncpg.Record) -> dict:
    client_id = json.loads(task["input"])["client_id"]
    actor = await _head_actor(pool, client_id)
    if actor is None:
        return {"skipped": "РОП не привязан к боту"}
    period = _period_yesterday(await _client_tz(pool, client_id))
    question = (
        f"Сформируй три темы для утренней планёрки на основе вчерашних звонков "
        f"(период {period['start']}–{period['end']}). Используй get_stats и "
        f"get_criteria_breakdown по всему отделу (manager_extension не указывай). "
        f"Если хочешь сказать что-то персонально про менеджера — сначала как обычно "
        f"проверь через verify_conclusion. Три коротких пункта, без вступлений, сразу по делу."
    )
    text = await rop_agent.answer(pool, actor, question)
    await _bot.send_message(actor.telegram_user_id, f"<b>☀️ На планёрку сегодня</b>\n\n{text}", parse_mode="HTML")
    return {"client_id": client_id, "chars": len(text)}


@register("rop_digest_weekly")
async def rop_digest_weekly(pool: asyncpg.Pool, task: asyncpg.Record) -> dict:
    client_id = json.loads(task["input"])["client_id"]
    actor = await _head_actor(pool, client_id)
    if actor is None:
        return {"skipped": "РОП не привязан к боту"}
    tz = await _client_tz(pool, client_id)
    last_week, week_before = _period_week(tz, 1), _period_week(tz, 2)
    question = (
        f"Сформируй сводку за прошедшую неделю ({last_week['start']}–{last_week['end']}) с "
        f"динамикой относительно недели до этого ({week_before['start']}–{week_before['end']}). "
        f"Используй compare_periods по всему отделу и get_criteria_breakdown за последнюю неделю. "
        f"Персональные выводы о менеджере — только через verify_conclusion. Кратко, по-деловому."
    )
    text = await rop_agent.answer(pool, actor, question)
    await _bot.send_message(actor.telegram_user_id, f"<b>📅 Сводка за неделю</b>\n\n{text}", parse_mode="HTML")
    return {"client_id": client_id, "chars": len(text)}


@register("rop_digest_monthly")
async def rop_digest_monthly(pool: asyncpg.Pool, task: asyncpg.Record) -> dict:
    client_id = json.loads(task["input"])["client_id"]
    actor = await _head_actor(pool, client_id)
    if actor is None:
        return {"skipped": "РОП не привязан к боту"}
    period = _period_prev_month(await _client_tz(pool, client_id))
    question = (
        f"Составь отчёт о прошедшем месяце ({period['start']}–{period['end']}) языком выручки и "
        f"встреч, а не критериев оценки звонков — не используй названия критериев вроде "
        f"«извлекающие вопросы», это не для читателя-нетехнического. Используй get_stats по всему "
        f"отделу. Если есть признак проблемы с качеством лидов — используй "
        f"get_lead_diagnosis_signal, но обязательно назови reviewed_calls и предупреди о смещённой "
        f"выборке, если она маленькая. 5-8 предложений, по-деловому."
    )
    text = await rop_agent.answer(pool, actor, question)
    await _bot.send_message(actor.telegram_user_id, f"<b>📈 Отчёт за месяц</b>\n\n{text}", parse_mode="HTML")
    return {"client_id": client_id, "chars": len(text)}
