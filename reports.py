"""
Рендер отчётов и отправка длинных сообщений — общее для demo_bot.py (ручной
аплоад) и worker.py (автосбор из Mango).
"""

import asyncio
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("reports")

SEVERITY_ICON = {"критично": "🔴", "заметно": "🟡", "мелочь": "⚪"}


def esc(s) -> str:
    return html.escape(str(s or ""))


def fmt_call_time(started_at: datetime | None, tz_name: str) -> str | None:
    """«14.09 13:43» — то же время, что показывает интерфейс Mango, для
    быстрого поиска звонка. started_at — TIMESTAMPTZ из calls.call_started_at
    (UTC-aware); None у звонков, загруженных до этого поля или вручную."""
    if started_at is None:
        return None
    local = started_at.astimezone(ZoneInfo(tz_name))
    return local.strftime("%d.%m %H:%M")


def unquote(s) -> str:
    s = str(s or "").strip()
    return s[1:-1].strip() if len(s) > 1 and s[0] in "«\"" and s[-1] in "»\"" else s


async def send_long(bot: Bot, chat_id: int, text: str) -> None:
    first = True
    while text:
        if not first:
            await asyncio.sleep(1)  # не больше 1 сообщения в секунду в один чат
        first = False
        cut = min(4000, len(text))
        if len(text) > 4000:
            cut = max(text.rfind("\n", 0, cut), 1)
        # parse_mode обязателен явно: в aiogram 3.7+ режим по умолчанию задаётся
        # только через DefaultBotProperties, и без него теги <b> уезжали в чат
        # текстом. Динамические куски в отчётах экранируются esc().
        await bot.send_message(chat_id, text[:cut], parse_mode="HTML")
        text = text[cut:].lstrip()


def render_short(short_report: dict, level: str | None, duration: float, manager_name: str | None = None,
                  call_time: str | None = None) -> str:
    """Единый короткий формат по каждому звонку — prompt_reports_final.md,
    п.2-4. Тот же вид для успешных и провальных, отличается только
    содержимым. manager_name=None — своя копия менеджеру (без имени в шапке),
    иначе — копия РОПу (п.7: «то же тело, плюс фамилия в шапке»). call_time —
    готовая строка вида «14.09 13:43», то же время, что в интерфейсе Mango —
    чтобы звонок можно было быстро найти и сверить с записью."""
    mm, ss = divmod(int(duration or 0), 60)
    level_str = level or "—"
    name_part = f"{esc(manager_name)} · " if manager_name else ""
    time_part = f"{esc(call_time)} · " if call_time else ""
    result = esc(short_report.get("result") or "")
    out = [f"📞 {name_part}{time_part}{mm}:{ss:02d} · {level_str} {result}".rstrip()]

    out += ["", "ХОРОШО"]
    for g in short_report.get("good") or [{"time": None, "text": "нечем"}]:
        prefix = f"{g['time']} " if g.get("time") else ""
        out.append(f"{prefix}{esc(g.get('text') or '')}")

    weak = short_report.get("weak") or []
    if weak:
        out += ["", "СЛАБО"]
        for w in weak:
            prefix = f"{w['time']} " if w.get("time") else ""
            out.append(f"{prefix}{esc(w.get('text') or '')}")
            if w.get("should_say"):
                out.append(f"     Надо было: «{esc(w['should_say'])}»")

    return "\n".join(out)


def detail_button(call_id: int, source: str = "pg", ready: bool = False) -> InlineKeyboardMarkup:
    """Кнопка под коротким отчётом. callback_data «detail:<source>:<id>» —
    source различает Postgres-звонки (автосбор из Mango, worker.py) и
    SQLite-звонки (ручной аплоад, demo_bot.py) — у них разные пространства
    id. Длина всегда далека от лимита Telegram в 64 байта на это поле."""
    text = "Разбор готов" if ready else "Подробный разбор"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=f"detail:{source}:{call_id}")]
    ])


async def send_short_report(bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup) -> None:
    """Короткий отчёт — одно сообщение с кнопкой, без разбивки на части
    (п.2-4: механизм отправки частями для него не используется). Цель —
    уложиться в 1200 символов; если модель всё же превысила лимит Telegram
    в 4000 — это нарушение инструкции промта, обрезаем по границе секции и
    логируем, чтобы не осталось незамеченным."""
    if len(text) > 4000:
        cut = text.rfind("\n\n", 0, 4000)
        if cut < 1:
            cut = 4000
        log.warning("короткий отчёт превысил 4000 символов (%d) — обрезан", len(text))
        text = text[:cut]
    await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


def render_head(result: dict, manager: str, duration: float, cost: float, manager_status: str,
                 call_time: str | None = None) -> str:
    score = result.get("score")
    score_str = f"{score}/100" if score is not None else "нет данных"
    mm, ss = divmod(int(duration), 60)
    fh = result.get("for_head") or {}
    headline = result.get("headline")
    time_part = f" · {esc(call_time)}" if call_time else ""

    out = [
        f"<b>📞 Разбор звонка</b> · {esc(manager)}{time_part} · {mm}:{ss:02d} · <b>{score_str}</b>",
        esc(unquote(headline)) if headline else "—",
    ]
    if result.get("call_cut_short"):
        out.append("⚠️ Звонок короткий или транскрипт обрывочный — данных мало")

    meeting = ("встреча: " + esc(result.get("meeting_details") or "да, время не уточнено")) \
        if result.get("meeting_booked") else "встреча НЕ назначена"
    out += ["", f"<b>Итог:</b> {meeting}",
            f"<b>Влияет на решение:</b> {'да' if result.get('influences_decision') else 'нет/не выяснено'}"]
    if result.get("gatekeeper_present"):
        out.append("<b>Секретарь:</b> был в разговоре")

    cliches = result.get("cliches") or []
    if cliches:
        out += ["", "<b>Штампы:</b> " + ", ".join("«" + esc(c) + "»" for c in cliches)]

    if fh.get("narrative"):
        out += ["", "<b>Как прошёл разговор</b>", esc(fh["narrative"])]

    # "Диагноз" звучит как готовый вердикт — заменено на осторожную
    # формулировку (prompt_reports_final, п.7). Внутренние категории
    # менеджер/скрипт/база из промта не меняются, наружу их подаём мягче.
    diag = fh.get("diagnosis") or {}
    if diag.get("type"):
        out += ["", "<b>В чём может быть дело:</b> " + esc(diag["type"])]
        if diag.get("reasoning"):
            out.append(esc(diag["reasoning"]))

    tp = fh.get("turning_point") or {}
    if tp.get("quote") or tp.get("what_happened"):
        out += ["", "<b>Поворотный момент</b>"]
        if tp.get("quote"):
            out.append("«" + esc(unquote(tp["quote"])) + "»")
        if tp.get("what_happened"):
            out.append(esc(tp["what_happened"]))
        if tp.get("alternative"):
            out.append("Стоило: " + esc(tp["alternative"]))

    gaps = fh.get("skill_gaps") or []
    if gaps:
        out += ["", "<b>Пробелы в навыках</b>"]
        for g in gaps:
            icon = SEVERITY_ICON.get(g.get("severity"), "•")
            out.append(f"{icon} {esc(g.get('skill'))}")
            if g.get("evidence"):
                out.append("   «" + esc(unquote(g["evidence"])) + "»")
            if g.get("recurring_risk"):
                out.append("   Риск: " + esc(g["recurring_risk"]))

    if fh.get("lead_quality"):
        out += ["", "<b>Качество лида:</b> " + esc(fh["lead_quality"])]
    if fh.get("lead_salvageable"):
        out.append("<b>Можно спасти:</b> " + esc(fh.get("salvage_action") or "да"))
    if fh.get("coaching_action"):
        out += ["", "<b>Действие на этой неделе:</b> " + esc(fh["coaching_action"])]

    out += ["", manager_status, f"<i>Стоимость: ${cost:.4f}</i>"]
    return "\n".join(out)


def render_manager(result: dict, duration: float, call_time: str | None = None) -> str:
    score = result.get("score")
    score_str = f"{score}/100" if score is not None else "нет данных"
    mm, ss = divmod(int(duration), 60)
    fm = result.get("for_manager") or {}
    headline = result.get("headline")
    time_part = f" · {esc(call_time)}" if call_time else ""

    out = [f"<b>📞 Разбор твоего звонка</b>{time_part} · {mm}:{ss:02d} · <b>{score_str}</b>"]
    if headline:
        out.append(esc(unquote(headline)))
    if result.get("call_cut_short"):
        out.append("⚠️ Запись короткая или обрывочная — разбор частичный")
    if fm.get("opening"):
        out += ["", esc(fm["opening"])]
    if result.get("meeting_booked"):
        out += ["", "✅ Встреча назначена: " + esc(result.get("meeting_details") or "")]

    did_well = fm.get("did_well") or []
    if did_well:
        out += ["", "<b>Что получилось</b>"]
        for d in did_well:
            out.append("• " + esc(d.get("what")))
            if d.get("quote"):
                out.append("   «" + esc(unquote(d["quote"])) + "»")
            if d.get("why"):
                out.append("   " + esc(d["why"]))

    moments = fm.get("moments") or []
    if moments:
        out += ["", "<b>Разбор моментов</b>"]
        for m in moments:
            out.append("«" + esc(unquote(m.get("quote"))) + "»")
            if m.get("reaction"):
                out.append("→ " + esc(m["reaction"]))
            if m.get("why"):
                out.append("Почему: " + esc(m["why"]))
            if m.get("say_instead"):
                out.append("Сказать вместо: «" + esc(unquote(m["say_instead"])) + "»")
            out.append("")

    if fm.get("key_mistake"):
        out.append("<b>Главная ошибка:</b> " + esc(fm["key_mistake"]))
    if fm.get("practice"):
        out.append("<b>Тренировка:</b> " + esc(fm["practice"]))
    if fm.get("next_call_focus"):
        out.append("<b>Фокус на ближайшие звонки:</b> " + esc(fm["next_call_focus"]))
    return "\n".join(out)
