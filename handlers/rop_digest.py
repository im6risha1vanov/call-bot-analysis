"""
Обработчики плановых дайджестов агента РОПа (Этап 3): 9:00 три темы на
планёрку, понедельник 10:00 сводка за неделю, 1 число 10:00 отчёт
собственнику. Задачи кладёт astra_worker.maybe_enqueue_rop_digests() —
агенты не общаются между собой напрямую, только через общую таблицу tasks.

Каждый обработчик просто прогоняет заранее сформулированный вопрос через тот
же rop_agent.answer(), что и живые вопросы в Telegram (единая логика, единая
проверка выборки и единый проверяющий проход).

Получатели — руководители отдела и владелец системы (role in head, owner).
Отдельной роли "владелец бизнеса" в employees нет: если месячный отчёт должен
уходить кому-то ещё, это отдельная задача — завести роль и привязку.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot

import rop_agent
from queue_runner import register
from tools import Actor, report_chat_ids

log = logging.getLogger("callbot-astra-worker.rop_digest")

_bot = Bot(token=os.environ["BOT_TOKEN"])


async def _head_actors(pool: asyncpg.Pool, client_id: int) -> list[Actor]:
    """Отчёт уходит каждому получателю: руководителям отдела и владельцу
    системы. Ответ агента считаем один раз (по первому из них) — данные и права
    у них одинаковые, а платить за один и тот же отчёт дважды незачем."""
    return [
        Actor(telegram_user_id=chat_id, client_id=client_id, role="head", extension=None)
        for chat_id in await report_chat_ids(pool, client_id)
    ]


async def _broadcast(actors: list[Actor], header: str, text: str) -> int:
    """Сбой доставки одному получателю не должен лишать отчёта остальных."""
    sent = 0
    for actor in actors:
        try:
            await _bot.send_message(actor.telegram_user_id, f"<b>{header}</b>\n\n{text}", parse_mode="HTML")
            sent += 1
        except Exception:
            log.exception("не удалось отправить отчёт руководителю id=%s", actor.telegram_user_id)
    return sent


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
    actors = await _head_actors(pool, client_id)
    if not actors:
        return {"skipped": "некому отправить: ни один получатель не привязан к боту"}
    period = _period_yesterday(await _client_tz(pool, client_id))
    question = (
        f"Сформируй три темы для утренней планёрки на основе вчерашних звонков "
        f"(период {period['start']}–{period['end']}). Используй get_stats и "
        f"get_criteria_breakdown по всему отделу (manager_extension не указывай). "
        f"Если хочешь сказать что-то персонально про менеджера — сначала как обычно "
        f"проверь через verify_conclusion. Три коротких пункта, без вступлений, сразу по делу."
    )
    text = await rop_agent.answer(pool, actors[0], question)
    sent = await _broadcast(actors, "☀️ На планёрку сегодня", text)
    return {"client_id": client_id, "chars": len(text), "sent_to": sent}


@register("rop_digest_weekly")
async def rop_digest_weekly(pool: asyncpg.Pool, task: asyncpg.Record) -> dict:
    client_id = json.loads(task["input"])["client_id"]
    actors = await _head_actors(pool, client_id)
    if not actors:
        return {"skipped": "некому отправить: ни один получатель не привязан к боту"}
    tz = await _client_tz(pool, client_id)
    last_week, week_before = _period_week(tz, 1), _period_week(tz, 2)
    question = (
        f"Сформируй сводку за прошедшую неделю ({last_week['start']}–{last_week['end']}) с "
        f"динамикой относительно недели до этого ({week_before['start']}–{week_before['end']}). "
        f"Используй compare_periods по всему отделу и get_criteria_breakdown за последнюю неделю. "
        f"Персональные выводы о менеджере — только через verify_conclusion. Кратко, по-деловому."
    )
    text = await rop_agent.answer(pool, actors[0], question)
    sent = await _broadcast(actors, "📅 Сводка за неделю", text)
    return {"client_id": client_id, "chars": len(text), "sent_to": sent}


@register("rop_digest_monthly")
async def rop_digest_monthly(pool: asyncpg.Pool, task: asyncpg.Record) -> dict:
    client_id = json.loads(task["input"])["client_id"]
    actors = await _head_actors(pool, client_id)
    if not actors:
        return {"skipped": "некому отправить: ни один получатель не привязан к боту"}
    period = _period_prev_month(await _client_tz(pool, client_id))
    question = (
        f"Составь отчёт о прошедшем месяце ({period['start']}–{period['end']}) языком выручки и "
        f"встреч, а не критериев оценки звонков — не используй названия критериев вроде "
        f"«извлекающие вопросы», это не для читателя-нетехнического. Используй get_stats по всему "
        f"отделу. Если есть признак проблемы с качеством лидов — используй "
        f"get_lead_diagnosis_signal, но обязательно назови reviewed_calls и предупреди о смещённой "
        f"выборке, если она маленькая. 5-8 предложений, по-деловому."
    )
    text = await rop_agent.answer(pool, actors[0], question)
    sent = await _broadcast(actors, "📈 Отчёт за месяц", text)
    return {"client_id": client_id, "chars": len(text), "sent_to": sent}


# ------------------------------------------------- вечерний дайджест РОПу
#
# Структура задана пользователем: 1) общая статистика по каждому менеджеру,
# 2) общие недочёты по отделу, 3) недочёты персональные, 4) отработка
# недочётов.
#
# Пункт 1 считает код: это точные числа, и модель к ним не подпускается —
# тот же принцип, что и с баллами. Пункты 2-4 — рассуждение, их собирает
# агент РОПа по тем же инструментам и с тем же правилом выборки.


async def _daily_stats(pool: asyncpg.Pool, client_id: int, day_start, day_end) -> list[dict]:
    """Всего звонков — абсолютно все за день, а не только разобранные: отбор
    отсеивает короткие и без записи, и руководителю важно видеть полную
    активность человека, а не долю, до которой добрался разбор."""
    rows = await pool.fetch(
        """
        SELECT c.extension,
               count(*) AS total,
               count(a.call_id) FILTER (WHERE a.status = 'analyzed') AS analyzed,
               count(*) FILTER (WHERE a.level = '✅') AS ok,
               count(*) FILTER (WHERE a.level = '⚠️') AS warn,
               count(*) FILTER (WHERE a.level = '❌') AS bad,
               count(*) FILTER (WHERE a.status = 'analyzed' AND a.level IS NULL) AS unrated
        FROM calls c
        LEFT JOIN astra_analysis a ON a.call_id = c.id
        WHERE c.client_id = $1 AND c.call_started_at >= $2 AND c.call_started_at < $3
          AND (c.raw_summary->>'context_type')::int = 2
        GROUP BY c.extension
        ORDER BY count(*) DESC
        """,
        client_id, day_start, day_end,
    )
    employees = await pool.fetch(
        "SELECT extension, full_name FROM employees WHERE client_id=$1 AND role='manager'", client_id)
    names = {e["extension"]: e["full_name"] for e in employees}
    return [
        {
            "manager": names.get(r["extension"]) or f"доб. {r['extension']}",
            "extension": r["extension"],
            "total": r["total"], "analyzed": r["analyzed"],
            "success": r["ok"], "warn": r["warn"], "failed": r["bad"], "unrated": r["unrated"],
        }
        for r in rows
    ]


def _render_stats(stats: list[dict]) -> str:
    out = ["<b>1. Статистика по менеджерам</b>"]
    for s in stats:
        out.append(
            f"• {s['manager']} — всего звонков {s['total']}, разобрано {s['analyzed']}: "
            f"✅ {s['success']} · ⚠️ {s['warn']} · ❌ {s['failed']}"
            + (f" · без оценки {s['unrated']}" if s["unrated"] else "")
        )
    out.append("<i>«Всего» — все исходящие звонки за день. В разбор идут те, что прошли "
               "отбор по длительности и с записью; «без оценки» — оборванные и автоответчики.</i>")
    return "\n".join(out)


@register("rop_digest_evening")
async def rop_digest_evening(pool: asyncpg.Pool, task: asyncpg.Record) -> dict:
    payload = json.loads(task["input"])
    client_id = payload["client_id"]
    actors = await _head_actors(pool, client_id)
    if not actors:
        return {"skipped": "некому отправить: ни один получатель не привязан к боту"}

    tz = await _client_tz(pool, client_id)
    # day в задаче — необязательный: планировщик его не ставит (отчёт за
    # сегодня), но он позволяет перегенерировать отчёт за прошедший день.
    day = (date.fromisoformat(payload["day"]) if payload.get("day")
           else datetime.now(ZoneInfo(tz)).date())
    day_start = datetime.combine(day, dtime.min, tzinfo=ZoneInfo(tz))
    day_end = day_start + timedelta(days=1)

    stats = await _daily_stats(pool, client_id, day_start, day_end)
    if not stats:
        return {"skipped": "за день не было исходящих звонков"}

    period = {"start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat()}
    question = (
        f"Собери вечерний отчёт за {period['start']} по трём разделам. Статистику "
        f"по менеджерам НЕ пиши — она уже посчитана кодом и будет добавлена перед твоим "
        f"текстом. Вот она, для опоры: {json.dumps(stats, ensure_ascii=False)}\n\n"
        f"Разделы, ровно в таком порядке и с такими заголовками:\n"
        f"2. Общие недочёты по отделу — что проваливается у всех или у большинства. "
        f"Используй get_criteria_breakdown по отделу за период "
        f"{period['start']}–{period['end']} (manager_extension не указывай). Схлопывай "
        f"связанные критерии в одну тему: если провалы вытекают один из другого, это одна "
        f"проблема, а не три.\n"
        f"3. Недочёты персональные — по каждому менеджеру, у кого есть чем отличиться от "
        f"остальных. Приводи ФАКТЫ (сколько из скольких, цитата из звонка через find_calls "
        f"или get_call), а оценочные суждения — только через verify_conclusion. Если у "
        f"человека данных на оценку мало, так и напиши, но факт приведи.\n"
        f"4. Отработка недочётов — что конкретно делать завтра. Для тренировки в тренажёре "
        f"указывай команду вида «/assign_train <добавочный> возражения» (отработка возражений "
        f"поштучно) или «/assign_train <добавочный> разговор» (звонок целиком). Если недочёт "
        f"общий — предложи разбор на планёрке с конкретными номерами звонков.\n\n"
        f"Без вступлений и без пересказа статистики. Заголовки разделов — обычным текстом "
        f"с номером, как указано выше."
    )
    text = await rop_agent.answer(pool, actors[0], question)
    body = f"{_render_stats(stats)}\n\n{text}"
    sent = await _broadcast(actors, "📊 Вечерний отчёт по отделу", body)
    return {"client_id": client_id, "managers": len(stats), "chars": len(body), "sent_to": sent}
