"""All arithmetic is deterministic. Unknown inputs stay unknown."""
from decimal import Decimal, InvalidOperation
import math


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("A number is required")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid number") from exc
    if not number.is_finite() or number < 0 or number.adjusted() > 308:
        raise ValueError("Nonnegative finite number required")
    return number


def calculate(inputs: dict) -> dict:
    values = {}; issues = []; assumptions = []
    units = {"channel_budget": "RUB", "applications": "applications", "contacts": "contacts",
             "processed_contacts": "contacts", "interested_leads": "interested_leads",
             "interest_rate": "fraction", "lead_to_sale_rate": "fraction", "average_check": "RUB",
             "margin": "fraction", "contact_cost": "RUB", "processing_cost": "RUB"}
    for key, unit in units.items():
        item = inputs.get(key)
        if item is None:
            continue
        if not isinstance(item, dict) or item.get("unit") != unit:
            issues.append(f"{key}: неверная единица"); continue
        try:
            value = _number(item.get("value"))
            if unit == "fraction" and value > 1:
                raise ValueError("Fraction must be at most one")
            if unit in {'contacts','applications','interested_leads'} and value != value.to_integral_value():
                raise ValueError('Counts must be integers')
        except ValueError:
            issues.append(f"{key}: неверное значение"); continue
        values[key] = value
        if item.get("assumption") is True:
            assumptions.append(key)
    result = dict.fromkeys(["current_application_cost", "interest_share", "sales", "revenue", "contribution", "net_result"])
    def compatible(*keys):
        rows = [inputs.get(k) or {} for k in keys]
        # Unknown period/channel does not justify silently transferring conversions.
        return (all(isinstance(r.get("period"), str) and r['period'] and isinstance(r.get("channel"), str) and r['channel'] for r in rows)
                and len({r["period"] for r in rows}) == 1
                and len({r["channel"] for r in rows}) == 1)
    if {"channel_budget", "applications"} <= values.keys():
        if values["applications"] and compatible("channel_budget", "applications"):
            result["current_application_cost"] = values["channel_budget"] / values["applications"]
        else:
            issues.append("Стоимость заявки: нулевой знаменатель или разные/неизвестные период и канал")
    if {"interested_leads", "processed_contacts"} <= values.keys():
        if values["processed_contacts"] and compatible("interested_leads", "processed_contacts") and values["interested_leads"] <= values["processed_contacts"]:
            result["interest_share"] = values["interested_leads"] / values["processed_contacts"]
        else:
            issues.append("Доля интереса: несовместимые входные данные")
    if {"contacts", "interest_rate", "lead_to_sale_rate"} <= values.keys():
        if compatible("contacts", "interest_rate", "lead_to_sale_rate"):
            result["sales"] = values["contacts"] * values["interest_rate"] * values["lead_to_sale_rate"]
        else:
            issues.append("Прогноз продаж: нельзя переносить конверсию между неизвестными/разными каналами и периодами")
    if result["sales"] is not None and "average_check" in values and compatible('contacts', 'average_check'):
        result["revenue"] = result["sales"] * values["average_check"]
    if result["revenue"] is not None and "margin" in values and compatible('contacts', 'margin'):
        result["contribution"] = result["revenue"] * values["margin"]
    included = inputs.get("processing_in_contact_cost")
    if result["contribution"] is not None and "contact_cost" in values and type(included) is bool and compatible('contacts', 'contact_cost'):
        if included:
            result["net_result"] = result["contribution"] - values["contact_cost"]
        elif "processing_cost" in values and compatible('contacts', 'processing_cost'):
            result["net_result"] = result["contribution"] - values["contact_cost"] - values["processing_cost"]
    safe = {}
    for key, value in result.items():
        number = float(value) if value is not None else None
        if number is not None and not math.isfinite(number):
            issues.append(f'{key}: результат за пределами допустимого числового диапазона')
            number = None
        safe[key] = number
    return {"values": safe,
            "issues": issues, "assumptions": assumptions, "forecast": result["sales"] is not None}
