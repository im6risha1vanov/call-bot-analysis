"""
Анализ звонка (аналог analysis.py второго бота, но на Astra через
ai.starimg.ru вместо Anthropic напрямую).

    score_call()        — извлечение фактов и проверка критериев, plus compute_level()
    short_report_call() — короткий отчёт (ХОРОШО/СЛАБО), всегда выполняется
    review_call()        — подробный разбор, только по кнопке «Подробный разбор»

Баллы считает Python, а не модель. Модель отвечает только на вопрос «пройден
критерий или нет» — это суждение. Сложение весов и нормализация по применимым
критериям выполняются кодом, поэтому арифметических расхождений между прогонами
быть не может в принципе.
"""

import json
import logging
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).parent
CVC_KEY = os.environ["CVC_API_KEY"]
CVC_BASE_URL = os.getenv("CVC_BASE_URL", "https://ai.starimg.ru/v1")
MODEL = os.getenv("CVC_MODEL", "gpt-6-astra")
REASONING_EFFORT = os.getenv("CVC_REASONING_EFFORT", "medium")
TIMEOUT = float(os.getenv("CVC_TIMEOUT_SECONDS", "600"))

# См. README: биллинг ai.starimg.ru не в долларах, а в "кредит-токенах" по
# приближённой формуле (свежий_вход + кеш*0.1 + output) * multiplier. Сверяйте
# периодически с реальными цифрами в личном кабинете.
ASTRA_MULTIPLIER = float(os.getenv("CVC_MODEL_MULTIPLIER", "6.5"))
CACHE_READ_FRACTION = float(os.getenv("CVC_CACHE_READ_FRACTION", "0.1"))

SCORE_PROMPT = (ROOT / "prompt_score.md").read_text(encoding="utf-8")
SHORT_PROMPT = (ROOT / "prompt_short.md").read_text(encoding="utf-8")
REVIEW_PROMPT = (ROOT / "prompt_review.md").read_text(encoding="utf-8")

# Веса живут здесь, а не в промте: так их можно менять, не трогая формулировки
# критериев и не рискуя сбить калибровку. Порядок задаёт приоритет разбора.
# Идентичны критериям Claude-бота — иначе сравнение уровней/баллов между
# движками было бы некорректным.
CRITERIA = [
    ("implication_questions",   14, "Извлекающие вопросы"),
    ("problem_over_situational", 11, "Проблемные вопросы преобладают"),
    ("explicit_need",           11, "Потребность доведена до явной"),
    ("decision_influence",      11, "Выяснено, кто влияет на решение"),
    ("brush_off_handled",       10, "Отговорка пройдена"),
    ("sold_meeting",             9, "Продавал встречу, а не продукт"),
    ("concrete_commitment",      9, "Конкретная договорённость"),
    ("call_reason",              8, "Обоснован повод звонка"),
    ("no_early_pitch",           7, "Не презентовал слишком рано"),
    ("insight",                  6, "Сказал что-то новое о бизнесе"),
    ("talk_share",               4, "Говорил меньше половины времени"),
    ("gatekeeper_passed",        8, "Прошёл секретаря"),
]


# ------------------------------------------------------------------ вызовы

def _parse(text: str) -> dict:
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    return json.loads(text.strip(), strict=False)


def _cost(usage: dict) -> float:
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens", 0)
    cache_write = details.get("cache_write_tokens", 0)
    fresh_input = max(usage.get("input_tokens", 0) - cached - cache_write, 0)
    billed = fresh_input + cached * CACHE_READ_FRACTION + cache_write + usage.get("output_tokens", 0)
    return billed * ASTRA_MULTIPLIER


async def _call(system: str, user: str, max_tokens: int):
    # temperature запрошена промтом (0 / 0.4), но reasoning-модели этого прокси
    # её игнорируют (API молча фиксирует temperature=1) — не передаём, чтобы не
    # создавать ложное впечатление, что она на что-то влияет.
    body = {
        "model": MODEL,
        "instructions": system,
        "input": user,
        "max_output_tokens": max_tokens,
        "reasoning": {"effort": REASONING_EFFORT},
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(
            CVC_BASE_URL + "/responses",
            headers={"Authorization": "Bearer " + CVC_KEY},
            json=body,
        )
    if r.status_code >= 300:
        logging.error("Astra %s: %s", r.status_code, r.text[:4000])
        raise RuntimeError(f"Astra {r.status_code}: {r.text[:700]}")
    data = r.json()
    text = (data.get("output_text") or "").strip()
    if not text:
        text = "".join(
            part.get("text", "")
            for item in data.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ).strip()
    usage = data.get("usage", {})
    cost = _cost(usage)
    logging.info("ТОКЕНЫ вход=%s выход=%s кеш=%s | %.0f ед.",
                 usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                 (usage.get("input_tokens_details") or {}).get("cached_tokens", 0), cost)
    return _parse(text), cost


# ------------------------------------------------------------------- баллы

def compute_score(scores: dict) -> tuple[int | None, list[dict]]:
    """Балл = набрано / применимо * 100. Считается здесь, не моделью.

    call_cut_short — особый случай: раньше здесь этой проверки не было, и
    обрывочный/слишком короткий звонок получал обычный числовой балл (часто
    0), хотя оценивать было нечего. Тот же класс бага, что чинили на
    Claude-стороне — портируем фикс, иначе сравнение уровней между движками
    на коротких звонках будет нечестным (Astra всегда покажет ❌, Claude —
    честное "нет данных")."""
    if scores.get("call_cut_short"):
        return None, []

    criteria = scores.get("criteria") or {}
    earned = applicable = 0
    rows = []

    for key, weight, title in CRITERIA:
        c = criteria.get(key) or {}
        is_applicable = c.get("applicable", True)
        passed = bool(c.get("passed"))

        if is_applicable:
            applicable += weight
            if passed:
                earned += weight

        rows.append({
            "key": key, "title": title, "weight": weight,
            "applicable": is_applicable, "passed": passed,
            "evidence": c.get("evidence", ""),
        })

    score = round(earned / applicable * 100) if applicable else None
    return score, rows


# Правило вердикта — в конфигурации, а не в коде: набор ключевых критериев и
# требуемое число провалов меняются через .env, логику для этого править не
# нужно.
#
# Прежнее правило («хватает одного ключевого или одного упущенного сигнала»)
# давало на реальных данных 86% ❌ и ноль ⚠️ — шкала из трёх уровней, где
# средний недостижим, это шкала из двух. Причина: критерий «выяснено, кто
# влияет на решение» проваливается в 90% звонков и в одиночку обваливал всё.
# Пороги подобраны не на глаз, а пересчётом сохранённых разборов — см.
# diagnose_verdicts.py.
LEVEL_CRITICAL_CRITERIA = set(
    (os.getenv("VERDICT_KEY_CRITERIA")
     or "brush_off_handled,no_early_pitch,decision_influence").replace(" ", "").split(",")
)

# Сколько ключевых критериев должно быть провалено для ❌.
VERDICT_MIN_FAILURES = int(os.getenv("VERDICT_MIN_FAILURES", "3"))

# Сколько провалов достаточно, если вдобавок упущен сигнал (названное третье
# лицо или согласие с проблемой, которые менеджер не отработал). Если задать
# значение не меньше VERDICT_MIN_FAILURES — сигналы на вердикт влиять
# перестанут.
VERDICT_MIN_FAILURES_WITH_SIGNAL = int(os.getenv("VERDICT_MIN_FAILURES_WITH_SIGNAL", "2"))


def compute_level(scores: dict, rows: list[dict]) -> str | None:
    if scores.get("call_cut_short"):
        return None
    if scores.get("meeting_booked") or scores.get("proposal_sent") or scores.get("decision_maker_contact"):
        return "✅"
    by_key = {r["key"]: r for r in rows}
    failed = sum(
        1 for key in LEVEL_CRITICAL_CRITERIA
        if key in by_key and by_key[key]["applicable"] and not by_key[key]["passed"]
    )
    signals = scores.get("signals") or {}
    missed_signal = (
        any(not t.get("followed_up") for t in signals.get("third_parties") or [])
        or any(not a.get("followed_up") for a in signals.get("problem_agreements") or [])
    )
    double = failed >= VERDICT_MIN_FAILURES or (missed_signal and failed >= VERDICT_MIN_FAILURES_WITH_SIGNAL)
    return "❌" if double else "⚠️"


def build_brief(scores: dict, rows: list[dict]) -> str:
    """Сводка для второго/третьего запроса: проваленное по убыванию веса + сигналы."""
    failed = [r for r in rows if r["applicable"] and not r["passed"]]
    failed.sort(key=lambda r: -r["weight"])

    lines = ["ПРОВАЛЕННЫЕ КРИТЕРИИ (по убыванию важности):"]
    for r in failed:
        lines.append(f"- {r['title']}: {r['evidence']}")
    if not failed:
        lines.append("- нет, все применимые критерии пройдены")

    s = scores.get("signals") or {}
    if s.get("third_parties"):
        lines.append("\nКЛИЕНТ УПОМЯНУЛ ТРЕТЬИХ ЛИЦ:")
        for t in s["third_parties"]:
            lines.append(f"- {t.get('who')}: «{t.get('quote')}»")
    if s.get("problem_agreements"):
        lines.append("\nКЛИЕНТ СОГЛАСИЛСЯ С ПРОБЛЕМОЙ:")
        for a in s["problem_agreements"]:
            lines.append(f"- «{a.get('quote')}» ({a.get('context')})")
    if s.get("unanswered_questions"):
        lines.append("\nВОПРОСЫ КЛИЕНТА БЕЗ ОТВЕТА:")
        lines += [f"- {q}" for q in s["unanswered_questions"]]

    q = scores.get("questions") or {}
    lines.append(
        f"\nВОПРОСЫ: ситуационных {q.get('situational', 0)}, "
        f"проблемных {q.get('problem', 0)}, "
        f"извлекающих {q.get('implication', 0)}. "
        f"Доля речи менеджера {scores.get('manager_talk_share', '?')}%."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- основное

async def score_call(transcript: str) -> tuple[dict, int | None, str | None, list[dict], float]:
    if not transcript.strip():
        return {}, None, None, [], 0.0
    scores, cost = await _call(SCORE_PROMPT, transcript, 3000)
    score, rows = compute_score(scores)
    level = compute_level(scores, rows)
    return scores, score, level, rows, cost


async def short_report_call(transcript: str, scores: dict, rows: list[dict], level: str | None) -> tuple[dict, float]:
    if not transcript.strip() or level is None:
        return {"result": "нет данных — звонок обрывочный или короче 30 секунд",
                "good": [{"time": None, "text": "нечем"}], "weak": []}, 0.0
    user = (
        f"УРОВЕНЬ: {level}\n\n"
        f"ТРАНСКРИПТ:\n{transcript}\n\n"
        f"---\n\nРЕЗУЛЬТАТЫ ПРОВЕРКИ:\n{build_brief(scores, rows)}"
    )
    return await _call(SHORT_PROMPT, user, 1500)


async def review_call(transcript: str, scores: dict, rows: list[dict]) -> tuple[dict, float]:
    """Подробный разбор — только по нажатию кнопки «Подробный разбор»;
    не запускается заранее ни для одного звонка."""
    user = (
        f"ТРАНСКРИПТ:\n{transcript}\n\n"
        f"---\n\nРЕЗУЛЬТАТЫ ПРОВЕРКИ:\n{build_brief(scores, rows)}"
    )
    return await _call(REVIEW_PROMPT, user, 5000)
