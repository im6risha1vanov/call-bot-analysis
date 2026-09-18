"""
Агент РОПа (Этап 3 мультиагентной спецификации). Один агент с циклом вызова
инструментов — не несколько агентов; координация между агентами на таких
задачах снижает качество, а не повышает (см. спецификацию).

Права проверяются в tools.py по Actor, который резолвится из
telegram_user_id ДО вызова агента и подставляется кодом при диспетчеризации
инструмента — модель никогда не видит и не может передать client_id/role/
extension сама (даже если попробует, сигнатуры функций их не принимают).

Проверяющий проход: любой вывод о конкретном менеджере обязан пройти через
инструмент verify_conclusion — отдельный, второй запрос к модели, который
решает confirmed/not_confirmed/insufficient_data. Финальный ответ
дополнительно сверяется кодом (см. _mentioned_unverified_managers): если в
тексте упомянут менеджер, по которому в разговоре запрашивались персональные
данные, но verify_conclusion для него не вернул confirmed — агента просят
переписать ответ, а не молча отправляют как есть.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import asyncpg
from anthropic import AsyncAnthropic

import tools
from tools import Actor, Forbidden

log = logging.getLogger("rop_agent")

MODEL = os.getenv("ROP_MODEL", "claude-sonnet-5")
MAX_ITERATIONS = 8

# Те же цены, что в /opt/callbot/analysis.py — одна и та же модель.
PRICE_IN_MTOK = 3.0
PRICE_OUT_MTOK = 15.0
PRICE_CACHE_WRITE_MTOK = 3.75
PRICE_CACHE_READ_MTOK = 0.30
DAILY_USD_LIMIT = float(os.getenv("ROP_DAILY_USD_LIMIT", "5.0"))

claude = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SAMPLING_RULE = (
    "О конкретном менеджере можно приводить ФАКТЫ из данных: сколько у него\n"
    "звонков, в скольких из них провален критерий, цитату из его разговора.\n"
    "Факт не требует большой выборки — «у Иванова 3 из 8 звонков без выяснения\n"
    "ЛПР» это измерение, а не вывод.\n\n"
    "Нельзя делать ОЦЕНОЧНЫХ суждений о человеке (работает хуже или лучше,\n"
    "слабое место, не умеет, просел, деградирует), если у него меньше 30\n"
    "разобранных звонков за рассматриваемый период. Не называй разницу между\n"
    "менеджерами значимой, если она меньше 20 процентных пунктов.\n\n"
    "Если оценка запрошена, а данных мало — прямо пиши «данных для оценки\n"
    "недостаточно» и сколько звонков нужно добрать; факты при этом привести\n"
    "можно и нужно. Не смягчай, не оговаривайся, не выдавай предположение за\n"
    "вывод."
)

SYSTEM_PROMPT_STATIC = f"""Ты — помощник руководителя отдела продаж (РОП) по разбору холодных звонков.
Отвечаешь на вопросы, делаешь выводы, предлагаешь решения на основе данных из
инструментов. Никогда не выдумывай цифры и факты — только то, что вернули
инструменты.

{SAMPLING_RULE}

Прежде чем написать ОЦЕНОЧНОЕ суждение, сравнение или рекомендацию,
касающуюся КОНКРЕТНОГО менеджера (не отдела в целом) — обязан вызвать
инструмент verify_conclusion с точной формулировкой и данными, на которые
опираешься. Если verdict не confirmed — не пиши это суждение в ответе вообще,
замени на «данных для оценки недостаточно» и сколько нужно добрать.

Голые факты о человеке (числа из инструментов, цитаты из его звонков)
verify_conclusion не требуют — проверять «3 из 8» нечего, это и есть данные.
Выводы про отдел в целом тоже не требуют.

get_lead_diagnosis_signal посчитан только по звонкам, где кто-то уже
запрашивал «Подробный разбор» — это смещённая выборка, не все звонки периода.
Если используешь этот сигнал — обязательно назови reviewed_calls и явно
предупреди о смещении выборки, никогда не подавай долю как статистику по
всем звонкам отдела.

get_successful_evidence отдаёт отдельные цитаты. Прежде чем предложить правку
скрипта, убедись, что похожий приём встретился у РАЗНЫХ менеджеров (сравни
manager_extension) — предложение по одной цитате от одного человека не годится
как "готовая правка".

Если данных недостаточно даже для одного инструмента — прямо скажи об этом,
не додумывай.

Отвечай на русском, по-деловому, без канцеляризмов и без markdown-таблиц.
Форматирование — обычный текст с HTML-тегами <b></b> для акцентов (это уйдёт
в Telegram с parse_mode=HTML), переносы строк — просто \\n.

В ответе не должно быть английских слов и служебных обозначений вообще. Читает
его руководитель отдела продаж, а не разработчик. Под запретом:

- названия инструментов (get_stats, find_calls, verify_conclusion и любые
  другие) — просто приводи данные, которые они вернули, не упоминая, откуда;
- ключи критериев (brush_off_handled, no_early_pitch, call_reason и прочие) —
  называй критерий его русским названием из списка ниже;
- служебные значения (confirmed, insufficient_data, drill, dialog, completed) —
  говори словами: «подтверждается», «данных для оценки недостаточно»,
  «отработка возражений», «разговор целиком», «завершена»;
- названия полей из данных (manager_extension, reviewed_calls, success_rate) —
  «добавочный», «звонков с подробным разбором», «доля успеха».

Конкретный звонок называй НОМЕРОМ КЛИЕНТА (поле client_phone), а при наличии
добавляй время: «звонок на +79061528719 в 14:35». Внутренний номер записи
(call_id) в ответе не показывай никогда: в интерфейсе Манго его нет, и найти по
нему звонок сотрудник не может. Номер клиента есть у каждого звонка, время —
примерно у половины, поэтому номер обязателен, а время — дополнение.

Русские названия критериев, которыми только и можно их называть:
извлекающие вопросы; проблемные вопросы преобладают; потребность доведена до
явной; выяснено, кто влияет на решение; отговорка пройдена; продавал встречу,
а не продукт; конкретная договорённость; обоснован повод звонка; не
презентовал слишком рано; сказал что-то новое о бизнесе; говорил меньше
половины времени; прошёл секретаря."""


def _system_prompt(today_iso: str, tz_name: str) -> str:
    return (
        SYSTEM_PROMPT_STATIC
        + f"\n\nСегодняшняя дата: {today_iso} (часовой пояс {tz_name}). Все относительные "
        + "периоды («этот месяц», «на прошлой неделе», «вчера», «в этом году») считай от "
        + "неё, а не от даты своего обучения — иначе получишь несуществующий период и пустые данные."
    )


VERIFY_CONCLUSION_SCHEMA = {
    "name": "verify_conclusion",
    "description": (
        "ОБЯЗАТЕЛЬНО вызови перед тем как написать в ответе вывод, оценку или сравнение "
        "про КОНКРЕТНОГО менеджера. claim — точная формулировка вывода; supporting_data — "
        "данные из уже вызванных инструментов, на которые вывод опирается. Не показывай "
        "вывод пользователю, если verdict не confirmed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "manager_extension": {"type": "string"},
            "claim": {"type": "string"},
            "supporting_data": {"type": "string"},
        },
        "required": ["manager_extension", "claim", "supporting_data"],
    },
}

TOOLS = tools.TOOL_SCHEMAS + [VERIFY_CONCLUSION_SCHEMA]

_TOOL_FUNCS = {
    "get_stats": tools.get_stats,
    "find_calls": tools.find_calls,
    "get_call": tools.get_call,
    "compare_periods": tools.compare_periods,
    "get_criteria_breakdown": tools.get_criteria_breakdown,
    "get_successful_evidence": tools.get_successful_evidence,
    "get_lead_diagnosis_signal": tools.get_lead_diagnosis_signal,
    "get_training_history": tools.get_training_history,
}


def _cost_of(usage) -> float:
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (
        usage.input_tokens / 1e6 * PRICE_IN_MTOK
        + usage.output_tokens / 1e6 * PRICE_OUT_MTOK
        + cache_write / 1e6 * PRICE_CACHE_WRITE_MTOK
        + cache_read / 1e6 * PRICE_CACHE_READ_MTOK
    )


async def _over_daily_limit(pool: asyncpg.Pool, client_id: int) -> bool:
    spent = await pool.fetchval(
        "SELECT spent_usd FROM rop_agent_daily_spend WHERE client_id=$1 AND day=$2",
        client_id, date.today(),
    )
    return float(spent or 0) >= DAILY_USD_LIMIT


async def _add_spend(pool: asyncpg.Pool, client_id: int, usd: float) -> None:
    await pool.execute(
        """
        INSERT INTO rop_agent_daily_spend (client_id, day, spent_usd) VALUES ($1, $2, $3)
        ON CONFLICT (client_id, day) DO UPDATE SET spent_usd = rop_agent_daily_spend.spent_usd + EXCLUDED.spent_usd
        """,
        client_id, date.today(), usd,
    )


async def _verify_conclusion(pool: asyncpg.Pool, actor: Actor, claim: str, supporting_data: str,
                              verified: set[str], manager_extension: str) -> dict:
    system = (
        "Тебе дано утверждение о конкретном сотруднике и данные, на которые оно опирается. "
        "Определи одним словом: confirmed — данные явно подтверждают утверждение; "
        "not_confirmed — данные противоречат утверждению или не относятся к нему; "
        "insufficient_data — данных недостаточно. Жёсткое правило: меньше 30 разобранных "
        "звонков на менеджера или разница между менеджерами меньше 20 процентных пунктов — "
        "всегда insufficient_data, даже если тебе кажется, что тенденция есть. "
        'Ответь строго JSON без markdown-обёртки: {"verdict": "confirmed|not_confirmed|insufficient_data", "note": "кратко почему"}.'
    )
    user = f"УТВЕРЖДЕНИЕ:\n{claim}\n\nДАННЫЕ:\n{supporting_data}"
    resp = await claude.messages.create(
        model=MODEL, max_tokens=300, thinking={"type": "disabled"},
        system=[{"type": "text", "text": system}],
        messages=[{"role": "user", "content": user}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    await _add_spend(pool, actor.client_id, _cost_of(resp.usage))
    try:
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(cleaned, strict=False)
    except Exception:
        log.warning("verify_conclusion: не удалось разобрать ответ модели: %r", raw[:300])
        result = {"verdict": "insufficient_data", "note": "не удалось проверить ответ модели"}
    if result.get("verdict") == "confirmed":
        verified.add(manager_extension)
    log.info("verify_conclusion доб.%s: %s (%s)", manager_extension, result.get("verdict"), result.get("note"))
    return result


async def _dispatch_tool(pool: asyncpg.Pool, actor: Actor, name: str, tool_input: dict,
                          verified: set[str]):
    if name == "verify_conclusion":
        return await _verify_conclusion(
            pool, actor, tool_input.get("claim", ""), tool_input.get("supporting_data", ""),
            verified, tool_input.get("manager_extension", ""),
        )
    fn = _TOOL_FUNCS.get(name)
    if fn is None:
        raise RuntimeError(f"неизвестный инструмент: {name}")
    # actor подставляется кодом, не моделью: даже если модель пришлёт свои
    # client_id/role в tool_input, ни одна из этих функций такой параметр не
    # принимает — лишний ключ просто упадёт с TypeError, а не тихо применится.
    return await fn(pool, actor, **tool_input)


async def _mentioned_unverified_managers(pool: asyncpg.Pool, actor: Actor, text: str,
                                          candidates: set[str], verified: set[str]) -> list[str]:
    pending = candidates - verified
    if not pending:
        return []
    employees = await pool.fetch(
        "SELECT extension, full_name FROM employees WHERE client_id=$1 AND role='manager'", actor.client_id
    )
    names = {e["extension"]: e["full_name"] for e in employees}
    hit = []
    for ext in pending:
        name = names.get(ext)
        if ext in text or (name and name in text) or f"доб. {ext}" in text or f"доб.{ext}" in text:
            hit.append(ext)
    return hit


async def answer(pool: asyncpg.Pool, actor: Actor, question: str) -> str:
    """Единая точка входа: и для свободных вопросов в Telegram, и для
    плановых дайджестов (см. handlers/rop_digest.py) — им соответствует
    заранее заготовленный текст question вместо вопроса живого человека."""
    if await _over_daily_limit(pool, actor.client_id):
        return "Дневной лимит бюджета агента РОПа на сегодня исчерпан, попробуйте завтра."

    client = await pool.fetchrow("SELECT timezone FROM clients WHERE id=$1", actor.client_id)
    tz_name = client["timezone"] if client else "Europe/Moscow"
    today_iso = datetime.now(ZoneInfo(tz_name)).date().isoformat()
    system_text = _system_prompt(today_iso, tz_name)

    messages: list[dict] = [{"role": "user", "content": question}]
    verified: set[str] = set()
    candidates: set[str] = set()

    for iteration in range(MAX_ITERATIONS):
        resp = await claude.messages.create(
            model=MODEL, max_tokens=2000, thinking={"type": "disabled"},
            system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS, messages=messages,
        )
        await _add_spend(pool, actor.client_id, _cost_of(resp.usage))
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            final_text = "".join(b.text for b in resp.content if b.type == "text").strip()
            bad = await _mentioned_unverified_managers(pool, actor, final_text, candidates, verified)
            if bad and iteration < MAX_ITERATIONS - 1:
                messages.append({"role": "user", "content": (
                    f"В ответе упомянуты добавочные {sorted(bad)} без подтверждённого "
                    f"verify_conclusion (verdict=confirmed). Либо вызови verify_conclusion "
                    f"для каждого такого вывода, либо перепиши ответ без невалидированных "
                    f"выводов об этих людях — например, приведи только цифры без оценочного "
                    f"суждения о человеке."
                )})
                continue
            if bad:
                log.warning("финальный ответ содержит неподтверждённые выводы о %s, отдаю как есть "
                            "(исчерпан лимит итераций)", bad)
            return final_text or "Не удалось сформировать ответ."

        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            block_input = block.input if isinstance(block.input, dict) else {}
            ext = block_input.get("manager_extension")
            if ext and block.name != "verify_conclusion":
                candidates.add(ext)
            try:
                result = await _dispatch_tool(pool, actor, block.name, block_input, verified)
                content = json.dumps(result, ensure_ascii=False, default=str)
            except Forbidden as exc:
                content = json.dumps({"error": str(exc)}, ensure_ascii=False)
            except Exception as exc:
                log.exception("ошибка инструмента %s", block.name)
                content = json.dumps({"error": f"внутренняя ошибка: {exc}"}, ensure_ascii=False)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
        messages.append({"role": "user", "content": tool_results})

    return "Не удалось получить ответ за отведённое число обращений к данным (8) — переформулируйте вопрос конкретнее."
