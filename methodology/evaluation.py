from __future__ import annotations

import json
import math
import re
from datetime import date
from decimal import Decimal

from . import economics
from .profiles import (ALLOWED_OUTCOMES, OUTCOMES, PRODUCTS, ROLES, STAGES, STATUS,
                       PRODUCT_LABELS, ROLE_LABELS, STAGE_LABELS, criteria_for)
from .runtime import ROOT, VERSION, approved_catalog, settings


class InvalidEvaluation(ValueError):
    pass


def _text(value, field, limit=3000):
    if value is not None and not isinstance(value, str):
        raise InvalidEvaluation(f"{field}: string required")
    return (value or "")[:limit]


def evidence_valid(quote, transcript):
    return isinstance(quote, str) and bool(quote.strip()) and " ".join(quote.split()) in " ".join(transcript.split())


def number_in_quote(value, quote, unit):
    try:
        expected = economics._number(value)
    except ValueError:
        return False
    text = re.sub(r'(?<=\d)[\s\u00a0](?=\d{3}(?:\D|$))', '', quote)
    for found in re.finditer(r'(?<!\w)(\d+(?:[.,]\d+)?)\s*(тыс\w*|млн\w*|миллион\w*|%|процент\w*)?', text, re.IGNORECASE):
        number = Decimal(found[1].replace(',', '.')); suffix = (found[2] or '').lower()
        if suffix.startswith('тыс'):
            number *= 1000
        elif suffix.startswith(('млн','миллион')):
            number *= 1000000
        elif suffix.startswith(('%','процент')):
            if unit != 'fraction':
                continue
            number /= 100
        if number == expected:
            return True
    return False


def validate_criterion(raw, transcript):
    if not isinstance(raw, dict) or raw.get("status") not in STATUS:
        raise InvalidEvaluation("Criterion status must be one of the four defined states")
    status = raw["status"]
    quote = _text(raw.get("evidence"), "evidence")
    reason = _text(raw.get("reason"), "reason")
    if status in {"passed", "failed"} and not evidence_valid(quote, transcript):
        return {"status": "insufficient_data", "evidence": "", "reason": "Цитата не подтверждена транскриптом"}
    return {"status": status, "evidence": quote, "reason": reason}


def verify_claim(raw, transcript, commercial_catalog):
    if not isinstance(raw, dict):
        raise InvalidEvaluation("Claim must be an object")
    claim = {k: raw.get(k) for k in ("product", "field", "value", "unit", "package", "call_center", "replacement_mode", "tax", "record_id")}
    claim["statement"] = _text(raw.get("statement"), "claim.statement")
    claim["evidence"] = _text(raw.get("evidence"), "claim.evidence")
    claim["verdict"] = "unverified"
    if claim['call_center'] is not None and type(claim['call_center']) is not bool:
        raise InvalidEvaluation('claim.call_center: boolean required')
    claim["reason"] = "Нет утверждённого условия для этого режима; это не основание штрафа"
    if not evidence_valid(claim["evidence"], transcript):
        claim["reason"] = "Утверждение не подтверждено цитатой"; return claim
    if claim['value'] is None:
        claim['reason'] = 'Значение условия не установлено'; return claim
    today = date.today().isoformat()
    for record in commercial_catalog.get("records", []):
        try:
            date.fromisoformat(record.get('effective_from', ''))
            if record.get('effective_until'):
                date.fromisoformat(record['effective_until'])
        except (ValueError, TypeError):
            continue
        if not (record.get("approved") is True and record.get("owner") and record.get("source")
                and record.get("effective_from") and record["effective_from"] <= today
                and (not record.get("effective_until") or today <= record["effective_until"])):
            continue
        if record.get("id") != claim.get("record_id"):
            continue
        # Price alone never identifies a product, unit, replacements or processing.
        keys = ("product", "unit", "package", "call_center", "replacement_mode", "tax")
        if any(record.get(k) != claim.get(k) for k in keys):
            continue
        field = claim.get("field")
        if field not in {"price", "demo", "guarantee", "installment", "data_origin", "launch_conditions"} or record.get(field) is None:
            continue
        equal = type(record[field]) is type(claim['value']) and record[field] == claim['value']
        if type(record[field]) in (int, float) and type(claim['value']) in (int, float):
            equal = Decimal(str(record[field])) == Decimal(str(claim['value']))
        claim["verdict"] = "confirmed" if equal else "contradicted"
        claim["reason"] = f"Сверено с утверждённой записью {record['id']}"
        break
    return claim


def evaluate(raw: dict, transcript: str, *, config=None, commercial_catalog=None, training_exercises=None) -> dict:
    if not isinstance(raw, dict):
        raise InvalidEvaluation("Model response must be an object")
    config = config or settings()
    classification = raw.get("classification")
    if not isinstance(classification, dict):
        raise InvalidEvaluation("Classification is required")
    mixed_drill = bool(training_exercises) and all(e['scenario'].get('mixed_drill') for e in training_exercises)
    if mixed_drill:
        classification = {**classification, 'sales_role': 'unknown', 'product': 'unknown', 'stage': 'unknown',
                          'confidence': 0, 'evidence': [], 'reason': 'Независимые упражнения разных профилей'}
    role, product, stage = (classification.get(k) for k in ("sales_role", "product", "stage"))
    if role not in ROLES or product not in PRODUCTS or stage not in STAGES:
        raise InvalidEvaluation("Unknown role/product/stage")
    confidence = classification.get("confidence")
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise InvalidEvaluation("Classification confidence must be in [0, 1]")
    evidence = classification.get("evidence")
    if not isinstance(evidence, list) or any(not isinstance(q, str) for q in evidence):
        raise InvalidEvaluation("Classification evidence must be a list of quotes")
    classification = {"sales_role": role, "product": product, "stage": stage, "confidence": confidence,
                      "reason": _text(classification.get("reason"), "classification.reason"),
                      "evidence": [q for q in evidence if evidence_valid(q, transcript)]}
    confident = (confidence >= config["classification_min_confidence"]
                 and bool(classification["evidence"]) and "unknown" not in (role, product, stage))
    titles = criteria_for(role, stage, confident)
    criteria = raw.get("criteria")
    if not isinstance(criteria, dict):
        raise InvalidEvaluation("Criteria must be an object")
    rows = []
    for key, title in titles.items():
        item = criteria.get(key, {"status": "insufficient_data", "reason": "Нет наблюдения"})
        value = validate_criterion(item, transcript)
        if key in {"clarity", "uses_client_response", "factual_accuracy", "respectful_communication"} and value["status"] == "not_applicable":
            value = {"status": "insufficient_data", "evidence": "", "reason": "Общий критерий требует наблюдения"}
        rows.append({"key": key, "title": title, **value})
    claims = raw.get("claims", [])
    if not isinstance(claims, list) or len(claims) > 100:
        raise InvalidEvaluation("Claims must be a bounded list")
    claims = [verify_claim(c, transcript, commercial_catalog or approved_catalog()) for c in claims]
    # An unapproved condition cannot produce a penalty through factual_accuracy.
    for row in rows:
        if row["key"] == "factual_accuracy" and row["status"] == "failed":
            if not any(c["verdict"] == "contradicted" for c in claims):
                contradiction = raw.get("factual_contradiction")
                if not (isinstance(contradiction, dict) and evidence_valid(contradiction.get("claim"), transcript)
                        and evidence_valid(contradiction.get("correction"), transcript)
                        and contradiction["claim"] != contradiction["correction"]):
                    row.update(status="insufficient_data", reason="Нет подтверждённого противоречия фактам")
    facts = raw.get("facts", [])
    if not isinstance(facts, list) or len(facts) > 100:
        raise InvalidEvaluation("Facts must be a bounded list")
    verified_facts = []
    for fact in facts:
        if not isinstance(fact, dict):
            raise InvalidEvaluation("Fact must be an object")
        for key in ('metric', 'unit', 'period', 'channel', 'source', 'evidence'):
            if fact.get(key) is not None and not isinstance(fact[key], str):
                raise InvalidEvaluation(f'fact.{key}: string required')
        if type(fact.get('assumption')) is not bool:
            raise InvalidEvaluation('fact.assumption: boolean required')
        for key in ('period', 'channel'):
            if isinstance(fact.get(key), str) and fact[key].strip().lower() in {'unknown','null','none','неизвестно','не указан','неизвестен',''}:
                fact[key] = None
        if fact.get('value') is not None and type(fact['value']) not in (str, int, float):
            raise InvalidEvaluation('fact.value: scalar required')
        numeric = fact.get('metric') in {'channel_budget','applications','contacts','processed_contacts','interested_leads','interest_rate','lead_to_sale_rate','average_check','margin','contact_cost','processing_cost'}
        if evidence_valid(fact.get("evidence"), transcript) and (not numeric or number_in_quote(fact.get('value'), fact['evidence'], fact.get('unit'))):
            verified_facts.append(fact)
    inputs = {}
    for fact in verified_facts:
        if fact.get("metric"):
            # Conflicting figures remain unknown; never silently pick the convenient one.
            key = fact["metric"]
            if key in inputs:
                inputs[key] = None
            else:
                inputs[key] = fact
    included = raw.get("processing_in_contact_cost")
    if isinstance(included, dict) and type(included.get("value")) is bool and evidence_valid(included.get("evidence"), transcript):
        inputs["processing_in_contact_cost"] = included["value"]
    economy = economics.calculate(inputs)
    outcome = raw.get("outcome")
    next_step = raw.get("next_step")
    if not isinstance(outcome, dict) or outcome.get("type") not in OUTCOMES:
        raise InvalidEvaluation("Outcome type is required")
    if not isinstance(next_step, dict) or type(next_step.get("agreed")) is not bool:
        raise InvalidEvaluation("Next step and boolean client consent are required")
    step = {k: _text(next_step.get(k), f"next_step.{k}") or None
            for k in ("action", "responsible", "deadline", "channel", "scheduled_time", "evidence")}
    step["agreed"] = next_step["agreed"]
    complete_step = bool(step["action"] and step["responsible"] and step["deadline"] and step["channel"]
                         and step["agreed"] and evidence_valid(step["evidence"], transcript))
    if outcome["type"] in {"meeting", "scheduled_callback"}:
        complete_step = complete_step and bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", step["scheduled_time"] or ""))
    allowed = ({"qualified_consultation", "scheduled_callback", "disqualified"}
               if role == "qualification_operator" else ALLOWED_OUTCOMES[stage])
    outcome = {"type": outcome["type"], "description": _text(outcome.get("description"), "outcome.description"),
               "evidence": _text(outcome.get("evidence"), "outcome.evidence"),
               "confirmed": bool(confident and outcome["type"] in allowed and complete_step
                                 and evidence_valid(outcome.get("evidence"), transcript))}
    objections = raw.get("objections", [])
    if not isinstance(objections, list):
        raise InvalidEvaluation("Objections must be a list")
    objections = [o for o in objections if isinstance(o, dict) and evidence_valid(o.get("evidence"), transcript)]
    counts = {s: sum(r["status"] == s for r in rows) for s in STATUS}
    measured = counts["passed"] + counts["failed"]
    coaching = raw.get("coaching") or {}
    if not isinstance(coaching, dict):
        raise InvalidEvaluation("Coaching must be an object")
    exercises = raw.get('exercise_results', [])
    if not isinstance(exercises, list) or len(exercises) > 5:
        raise InvalidEvaluation('Exercise results must contain at most five items')
    checked_exercises = []
    seen = set()
    exercise_contexts = {e['scenario']['exercise']: e for e in (training_exercises or [])}
    for exercise in exercises:
        if not isinstance(exercise, dict) or type(exercise.get('exercise')) is not int or not 1 <= exercise['exercise'] <= 5 or exercise['exercise'] in seen:
            raise InvalidEvaluation('Invalid exercise index')
        seen.add(exercise['exercise'])
        exercise_titles, exercise_transcript, metadata = titles, transcript, {}
        if training_exercises is not None:
            source = exercise_contexts.get(exercise['exercise'])
            if not source:
                raise InvalidEvaluation('Exercise must match an answered exercise')
            scenario = source['scenario']
            if scenario['role'] not in ROLES or scenario['stage'] not in STAGES or scenario['product'] not in PRODUCTS:
                raise InvalidEvaluation('Invalid pinned exercise profile')
            exercise_titles = criteria_for(scenario['role'], scenario['stage'], True)
            exercise_transcript = 'Клиент: ' + source['objection'] + '\nМенеджер: ' + source['answer']
            metadata = {'scenario_title': scenario['title'], 'sales_role': scenario['role'],
                        'product': scenario['product'], 'stage': scenario['stage']}
        criterion = exercise.get('criterion')
        if criterion not in exercise_titles:
            raise InvalidEvaluation('Exercise criterion must be applicable to its profile')
        checked_exercises.append({'exercise': exercise['exercise'], 'criterion': criterion, 'title': exercise_titles[criterion],
                                  **metadata, **validate_criterion(exercise, exercise_transcript),
                                  'say_instead': _text(exercise.get('say_instead'), 'exercise.say_instead', 600)})
    return {"methodology_version": VERSION, "catalog_version": (commercial_catalog or approved_catalog())["version"],
            "mixed_drill": mixed_drill,
            "classification": classification, "narrow_profile_applied": confident, "rows": rows,
            "quality": {"counts": counts, "observed": measured, "score": None, "level": None,
                        "calibrated": False, "coverage": round(measured / len(rows), 3) if rows else 0},
            "outcome": outcome, "next_step": step, "facts": verified_facts, "objections": objections,
            "commercial_claims": claims, "honesty": {"contradicted_claims": sum(c["verdict"] == "contradicted" for c in claims),
                                                      "unverified_claims": sum(c["verdict"] == "unverified" for c in claims)},
            "economics": economy, "exercise_results": checked_exercises, "coaching": {"say_instead": _text(coaching.get("say_instead"), "coaching.say_instead", 600),
                                               "repeat_scenario": _text(coaching.get("repeat_scenario"), "coaching.repeat_scenario", 600)}}


def score_prompt() -> str:
    from .profiles import COMMON, OPERATOR_CRITERIA, PROFILE_CRITERIA, PRODUCT_RULES
    schema = {"roles": sorted(ROLES), "products": sorted(PRODUCTS), "stages": sorted(STAGES),
              "common_criteria": COMMON, "profile_criteria": PROFILE_CRITERIA,
              "operator_criteria": OPERATOR_CRITERIA, "product_rules": PRODUCT_RULES, "outcomes": sorted(OUTCOMES)}
    return ((ROOT.parent / "prompts/course/score.md").read_text()
            + "\n\nСХЕМА ПРОФИЛЕЙ:\n" + json.dumps(schema, ensure_ascii=False)
            + "\n\nУТВЕРЖДЁННЫЕ КОММЕРЧЕСКИЕ УСЛОВИЯ:\n" + json.dumps(approved_catalog(), ensure_ascii=False))


def render(result: dict, detailed=False) -> str:
    c = result["classification"]; counts = result["quality"]["counts"]
    lines = ['Короткая отработка: разные ситуации' if result.get('mixed_drill') else
             f"{ROLE_LABELS[c['sales_role']]} · {PRODUCT_LABELS[c['product']]} · {STAGE_LABELS[c['stage']]}",
             f"Качество: выполнено {counts['passed']}, не выполнено {counts['failed']}, "
             f"недостаточно данных {counts['insufficient_data']}, неприменимо {counts['not_applicable']}.",
             f"Результат: {result['outcome']['description'] or 'не описан'}.",
             "Следующий шаг подтверждён." if result["outcome"]["confirmed"] else "Подтверждённый следующий шаг не установлен."]
    if result.get('mixed_drill'):
        lines.append('Каждое упражнение оценено по его ситуации и этапу.')
    elif not result["narrow_profile_applied"]:
        lines.append("Профиль определён неуверенно; проверены только общие навыки.")
    good = [r for r in result["rows"] if r["status"] == "passed"][:2]
    priority = {'factual_accuracy':0,'respectful_communication':1,'next_step':2}
    weak = sorted([r for r in result['rows'] if r['status']=='failed'],key=lambda r:priority.get(r['key'],3))[:1]
    for row in good:
        lines.append(f"Получилось: {row['title']} — «{row['evidence']}».")
    if len(good)<2:
        lines.append('Материала недостаточно для двух подтверждённых сильных действий.')
    for row in weak:
        lines.append(f"Главная задача: {row['title']} — «{row['evidence']}». {row['reason']}")
    if weak and result["coaching"]["say_instead"]:
        lines.append("Попробуйте: " + result["coaching"]["say_instead"])
    if result["coaching"]["repeat_scenario"]:
        lines.append("Повторите ситуацию: " + result["coaching"]["repeat_scenario"])
    if result["honesty"]["unverified_claims"]:
        lines.append("Есть коммерческие утверждения, для которых нужно подтверждение действующих условий.")
    if result["honesty"]["contradicted_claims"]:
        lines.append("Есть противоречие утверждённым условиям; коммерческий результат его не отменяет.")
    if detailed:
        for exercise in result.get('exercise_results', []):
            label = {'passed':'выполнено','failed':'не выполнено','not_applicable':'неприменимо','insufficient_data':'недостаточно данных'}[exercise['status']]
            situation = ' — ' + exercise['scenario_title'] if exercise.get('scenario_title') else ''
            lines.append(f"\nУпражнение {exercise['exercise']}{situation}, {exercise['title']}: {label}. {exercise['reason']}\n{exercise['evidence']}")
            if exercise['status'] == 'failed' and exercise['say_instead']:
                lines.append('Попробуйте: ' + exercise['say_instead'])
        for row in result["rows"]:
            label = {"passed": "выполнено", "failed": "не выполнено", "not_applicable": "неприменимо", "insufficient_data": "недостаточно данных"}[row["status"]]
            lines.append(f"\n{row['title']}: {label}. {row['reason']}\n{row['evidence']}")
        for key, value in result["economics"]["values"].items():
            if value is not None:
                label = {"current_application_cost": "Стоимость заявки", "interest_share": "Доля интереса", "sales": "Оценка продаж",
                         "revenue": "Оценка выручки", "contribution": "Маржинальный доход", "net_result": "Результат после расходов"}[key]
                unit = ' ₽' if key in {'current_application_cost', 'revenue', 'contribution', 'net_result'} else (' продаж' if key == 'sales' else ' (доля)')
                lines.append(f"{label}: {value:g}{unit}")
        if result['economics']['forecast']:
            lines.append('Расчёт — оценка по заданным входным данным, а не гарантия продукта.')
        if result['economics']['assumptions']:
            lines.append('В расчёте есть предположения; проверьте исходные данные.')
        lines.extend(result["economics"]["issues"])
    template = ROOT.parent / 'prompts/course' / ('review.md' if detailed else 'short.md')
    return template.read_text().format(body='\n'.join(lines)).strip()
