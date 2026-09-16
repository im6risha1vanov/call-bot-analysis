"""
Обработчик задачи `oversight_report` (Этап 5) — ежедневный отчёт владельцу
системы в 22:00.

Метрики считает oversight.py обычным SQL. Модель вызывается ровно один раз и
только чтобы превратить готовые цифры в читаемый текст — и то лишь когда есть
о чём говорить: сутки без сбоев отдаются одной строкой, без обращения к
модели.
"""

import json
import logging
import os

import asyncpg
from aiogram import Bot

import oversight
from queue_runner import register

log = logging.getLogger("queue-runner.oversight_report")

MODEL = os.getenv("ROP_MODEL", "claude-sonnet-5")
QUIET_SPEND_USD = float(os.getenv("OVERSIGHT_QUIET_SPEND_USD", "3.0"))

_bot = Bot(token=os.environ["BOT_TOKEN"])

SYSTEM_PROMPT = """Ты составляешь короткий ежедневный отчёт о работе системы разбора холодных
звонков для её владельца — технического человека, который хочет за десять секунд понять,
нужно ли вмешиваться.

Тебе дают готовые метрики за сутки в JSON. Ничего не досчитывай и не придумывай — используй
только то, что есть в данных.

Структура ответа:
- Первая строка — вердикт одной фразой: работает штатно / есть сбои / требует вмешательства.
- Что работало: типы задач и сколько выполнено.
- Что падало: тип задачи, сколько раз, текст ошибки человеческим языком (не копируй трейсбек целиком).
- Сколько потрачено.
- На что обратить внимание — только если в данных есть основание. Если оснований нет, эту секцию не пиши.

Пиши по-русски, без markdown-заголовков, без эмодзи кроме одного в начале первой строки.
Не растекайся: чем спокойнее сутки, тем короче отчёт."""


async def _owner_chat_id(pool: asyncpg.Pool) -> int | None:
    """Владелец системы — тот, кому уходит технический отчёт. По умолчанию
    HEAD_CHAT_ID из .env (он уже используется для системных сообщений);
    OVERSIGHT_CHAT_ID позволяет развести владельца системы и РОПа, если это
    разные люди."""
    explicit = os.getenv("OVERSIGHT_CHAT_ID") or os.getenv("HEAD_CHAT_ID")
    if explicit and explicit.strip():
        return int(explicit.strip())
    row = await pool.fetchrow("SELECT telegram_user_id FROM employees WHERE role='head' LIMIT 1")
    return row["telegram_user_id"] if row else None


async def _render_with_model(metrics: dict) -> tuple[str, float]:
    from anthropic import AsyncAnthropic

    claude = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = await claude.messages.create(
        model=MODEL, max_tokens=1000, thinking={"type": "disabled"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(metrics, ensure_ascii=False, indent=2)}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    usage = resp.usage
    # Прайс sonnet: $3/1M входных, $15/1M выходных.
    cost = usage.input_tokens / 1e6 * 3 + usage.output_tokens / 1e6 * 15
    log.info("отчёт надзора: вход=%s выход=%s ≈$%.4f", usage.input_tokens, usage.output_tokens, cost)
    return text, cost


@register("oversight_report")
async def oversight_report(pool: asyncpg.Pool, task: asyncpg.Record) -> dict:
    metrics = await oversight.collect(pool)
    quiet = oversight.is_quiet(metrics, QUIET_SPEND_USD)

    if quiet:
        text = oversight.render_quiet_line(metrics)
        cost = 0.0
    else:
        text, cost = await _render_with_model(metrics)
        if metrics["queue"]["awaiting_approval"]:
            text += (f"\n\nЖдут подтверждения: {metrics['queue']['awaiting_approval']} — "
                     f"посмотреть и решить: /approvals")

    chat_id = await _owner_chat_id(pool)
    if chat_id:
        try:
            await _bot.send_message(chat_id, text)
        except Exception:
            log.exception("не удалось отправить отчёт надзора")
    else:
        log.warning("некому отправить отчёт надзора: не задан OVERSIGHT_CHAT_ID/HEAD_CHAT_ID и нет head в employees")

    return {
        "quiet": quiet,
        "cost_usd": round(cost, 4),
        "failures": len(metrics["failures"]),
        "stuck": len(metrics["stuck"]),
        "limit_hits": len(metrics["limit_hits"]),
    }
