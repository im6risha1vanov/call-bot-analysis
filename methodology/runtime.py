from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = "course_2025_v1"
LEGACY_VERSION = "legacy_52e4d575"


def settings() -> dict:
    """Read on every operation: disabling does not require a process restart."""
    try:
        value = json.loads((ROOT / "config.json").read_text())
        if value.get("mode") not in {"legacy", "shadow"}:
            raise ValueError("Only legacy and shadow modes are available before calibration")
        if value.get("version") != VERSION:
            raise ValueError("Unknown methodology version")
        confidence = value.get("classification_min_confidence")
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
            raise ValueError("Invalid classification confidence")
        if type(value.get("training_enabled")) is not bool:
            raise ValueError("Invalid training switch")
        budget = value.get('daily_budget_units')
        if type(budget) not in (int, float) or not 0 <= budget <= 2000000:
            raise ValueError('Invalid methodology budget')
        return value
    except (OSError, ValueError, TypeError):
        logging.getLogger(__name__).error("Methodology configuration invalid; using legacy mode")
        return {"mode": "legacy", "version": VERSION, "training_enabled": False,
                "classification_min_confidence": 0.75, "calibrated": False, "daily_budget_units": 0}


def shadow_enabled() -> bool:
    return settings()["mode"] == "shadow"


def training_enabled() -> bool:
    config = settings()
    return config["mode"] == "shadow" and config["training_enabled"]


def catalog() -> dict:
    return json.loads((ROOT / "catalog.json").read_text())


def approved_catalog() -> dict:
    try:
        data = catalog()
        if not isinstance(data, dict) or not isinstance(data.get('version'), str) or not isinstance(data.get('records'), list) or any(not isinstance(r,dict) for r in data['records']):
            raise ValueError('Invalid catalog')
    except (OSError, ValueError, TypeError):
        logging.getLogger(__name__).error('Commercial catalog invalid; no conditions are approved')
        return {'version':'unavailable','records':[]}
    # Approval requires traceability. Availability of a course page is not approval.
    valid = []
    today = date.today()
    ids = [r.get('id') for r in data['records']]
    for record in data['records']:
        if record.get('approved') is not True or not record.get('owner') or not record.get('source') or not record.get('id') or ids.count(record['id']) != 1:
            continue
        try:
            starts = date.fromisoformat(record.get('effective_from', ''))
            ends = date.fromisoformat(record['effective_until']) if record.get('effective_until') else date.max
        except (ValueError, TypeError):
            continue
        if starts <= today <= ends:
            valid.append(record)
    data['records'] = valid
    return data
