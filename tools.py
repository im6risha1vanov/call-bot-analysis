"""
Слой инструментов (Этап 2 мультиагентной спецификации). Функции доступа к
данным, которые переиспользуют агент РОПа (Этап 3) и тренажёр (Этап 4) —
пишутся один раз и аккуратно, а не заново в каждом агенте.

Права проверяются ВНУТРИ каждой функции, по действующему лицу (Actor),
которое код передаёт первым аргументом сам — модель его не видит и не может
повлиять на него формулировкой промпта. Менеджер получает только свои звонки
независимо от того, что запросила модель.

Транскрипт целиком отдаёт только get_call — остальные функции возвращают
компактные структуры, а не сырые строки базы.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from typing import Literal
from zoneinfo import ZoneInfo

import asyncpg


class Forbidden(Exception):
    """Действующему лицу не положен доступ к запрошенным данным."""


@dataclass(frozen=True)
class Actor:
    """Тот, от чьего имени вызывается инструмент. client_id, role и extension
    получены из employees по telegram_user_id — не из параметров, которые
    прислала модель."""
    telegram_user_id: int
    client_id: int
    role: Literal["head", "manager"]
    extension: str | None  # None для role == "head"


async def resolve_actor(pool: asyncpg.Pool, telegram_user_id: int) -> Actor | None:
    row = await pool.fetchrow(
        "SELECT client_id, role, extension FROM employees WHERE telegram_user_id=$1",
        telegram_user_id,
    )
    if row is None:
        return None
    return Actor(telegram_user_id=telegram_user_id, client_id=row["client_id"],
                 role=row["role"], extension=row["extension"])


def _scope_extension(actor: Actor, requested: str | None) -> str | None:
    """Единственное место, решающее, чьи звонки видно. Менеджер всегда видит
    только себя — параметр requested для него игнорируется полностью, даже
    если он указывает добавочный другого человека."""
    if actor.role == "manager":
        return actor.extension
    return requested


async def _require_client(pool: asyncpg.Pool, client_id: int) -> asyncpg.Record:
    client = await pool.fetchrow("SELECT * FROM clients WHERE id=$1", client_id)
    if client is None:
        raise RuntimeError(f"clients.id={client_id} не найден")
    return client


def _period_bounds(tz_name: str, period: dict) -> tuple[datetime, datetime]:
    """period = {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} — конец не
    включается, обе даты в таймзоне клиента."""
    tz = ZoneInfo(tz_name)
    start = date.fromisoformat(period["start"])
    end = date.fromisoformat(period["end"])
    return (
        datetime.combine(start, dtime.min, tzinfo=tz),
        datetime.combine(end, dtime.min, tzinfo=tz),
    )


def _weak_point(short_report_json) -> str | None:
    if not short_report_json:
        return None
    weak = (json.loads(short_report_json).get("weak") or [None])[0]
    return weak.get("text") if weak else None


def _level_counts(rows) -> dict[str, int]:
    counts = {"✅": 0, "❌": 0, "❌❌": 0}
    for r in rows:
        if r["level"] in counts:
            counts[r["level"]] += 1
    return counts


# --------------------------------------------------------------- get_stats

async def get_stats(pool: asyncpg.Pool, actor: Actor, period: dict,
                     manager_extension: str | None = None) -> dict:
    extension = _scope_extension(actor, manager_extension)
    client = await _require_client(pool, actor.client_id)
    start, end = _period_bounds(client["timezone"], period)

    rows = await pool.fetch(
        """
        SELECT a.level FROM astra_analysis a JOIN calls c ON c.id = a.call_id
        WHERE c.client_id = $1 AND a.status = 'analyzed'
          AND a.updated_at >= $2 AND a.updated_at < $3
          AND ($4::text IS NULL OR c.extension = $4)
        """,
        actor.client_id, start, end, extension,
    )
    counts = _level_counts(rows)
    total = len(rows)
    return {
        "period": period,
        "extension": extension,
        "total_calls": total,
        "levels": counts,
        "success_rate": round(counts["✅"] / total, 3) if total else None,
    }


# ------------------------------------------------------------- find_calls

async def find_calls(pool: asyncpg.Pool, actor: Actor, filters: dict) -> list[dict]:
    extension = _scope_extension(actor, filters.get("manager_extension"))
    period = filters.get("period")
    start, end = (None, None)
    if period:
        client = await _require_client(pool, actor.client_id)
        start, end = _period_bounds(client["timezone"], period)
    level = filters.get("level")
    limit = min(int(filters.get("limit", 20)), 100)

    rows = await pool.fetch(
        """
        SELECT c.id, c.extension, c.call_started_at, c.duration_seconds, a.level, a.short_report
        FROM astra_analysis a JOIN calls c ON c.id = a.call_id
        WHERE c.client_id = $1 AND a.status = 'analyzed'
          AND ($2::text IS NULL OR c.extension = $2)
          AND ($3::timestamptz IS NULL OR a.updated_at >= $3)
          AND ($4::timestamptz IS NULL OR a.updated_at < $4)
          AND ($5::text IS NULL OR a.level = $5)
        ORDER BY c.call_started_at DESC
        LIMIT $6
        """,
        actor.client_id, extension, start, end, level, limit,
    )
    return [
        {
            "call_id": r["id"],
            "manager_extension": r["extension"],
            "started_at": r["call_started_at"].isoformat() if r["call_started_at"] else None,
            "duration_seconds": r["duration_seconds"],
            "level": r["level"],
            "weak_point": _weak_point(r["short_report"]),
        }
        for r in rows
    ]


# ---------------------------------------------------------------- get_call

async def get_call(pool: asyncpg.Pool, actor: Actor, call_id: int) -> dict:
    row = await pool.fetchrow(
        """
        SELECT c.id, c.client_id, c.extension, c.call_started_at, c.duration_seconds,
               c.transcript, a.level, a.analysis, a.short_report, a.detailed_report
        FROM calls c LEFT JOIN astra_analysis a ON a.call_id = c.id
        WHERE c.id = $1
        """,
        call_id,
    )
    if row is None or row["client_id"] != actor.client_id:
        raise Forbidden(f"звонок {call_id} недоступен")
    if actor.role == "manager" and row["extension"] != actor.extension:
        raise Forbidden(f"звонок {call_id} принадлежит другому менеджеру")

    return {
        "call_id": row["id"],
        "manager_extension": row["extension"],
        "started_at": row["call_started_at"].isoformat() if row["call_started_at"] else None,
        "duration_seconds": row["duration_seconds"],
        "level": row["level"],
        "transcript": row["transcript"],
        "analysis": json.loads(row["analysis"]) if row["analysis"] else None,
        "short_report": json.loads(row["short_report"]) if row["short_report"] else None,
        "detailed_report": json.loads(row["detailed_report"]) if row["detailed_report"] else None,
    }


# --------------------------------------------------------- compare_periods

async def compare_periods(pool: asyncpg.Pool, actor: Actor, period_a: dict, period_b: dict,
                           manager_extension: str | None = None) -> dict:
    a = await get_stats(pool, actor, period_a, manager_extension)
    b = await get_stats(pool, actor, period_b, manager_extension)
    return {"period_a": a, "period_b": b}


# --------------------------------------------------------- criteria breakdown

async def get_criteria_breakdown(pool: asyncpg.Pool, actor: Actor, period: dict,
                                  manager_extension: str | None = None) -> dict:
    extension = _scope_extension(actor, manager_extension)
    client = await _require_client(pool, actor.client_id)
    start, end = _period_bounds(client["timezone"], period)

    rows = await pool.fetch(
        """
        SELECT a.analysis FROM astra_analysis a JOIN calls c ON c.id = a.call_id
        WHERE c.client_id = $1 AND a.status = 'analyzed'
          AND a.updated_at >= $2 AND a.updated_at < $3
          AND ($4::text IS NULL OR c.extension = $4)
          AND a.analysis IS NOT NULL
        """,
        actor.client_id, start, end, extension,
    )
    total: dict[str, int] = {}
    failed: dict[str, int] = {}
    for r in rows:
        for row in (json.loads(r["analysis"]).get("rows") or []):
            if not row.get("applicable"):
                continue
            title = row["title"]
            total[title] = total.get(title, 0) + 1
            if not row.get("passed"):
                failed[title] = failed.get(title, 0) + 1

    criteria = [
        {"criterion": title, "applicable": n, "failed": failed.get(title, 0),
         "fail_rate": round(failed.get(title, 0) / n, 3)}
        for title, n in total.items()
    ]
    criteria.sort(key=lambda x: -x["fail_rate"])
    return {"period": period, "extension": extension, "sample_size": len(rows), "criteria": criteria}


# ------------------------------------------------------------- схемы для LLM

_PERIOD_SCHEMA = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "format": "date", "description": "Начало периода, включительно, YYYY-MM-DD"},
        "end": {"type": "string", "format": "date", "description": "Конец периода, не включая эту дату, YYYY-MM-DD"},
    },
    "required": ["start", "end"],
}

TOOL_SCHEMAS = [
    {
        "name": "get_stats",
        "description": "Сводная статистика по звонкам за период: сколько всего, разбивка по уровням ✅/❌/❌❌, доля успеха.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": _PERIOD_SCHEMA,
                "manager_extension": {"type": ["string", "null"],
                                       "description": "Добавочный менеджера, если нужна статистика по одному человеку; null — по всему отделу (только для РОПа)"},
            },
            "required": ["period"],
        },
    },
    {
        "name": "find_calls",
        "description": "Список звонков по фильтрам: период, менеджер, уровень. Без полного транскрипта — только сводка по каждому звонку.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": _PERIOD_SCHEMA,
                "manager_extension": {"type": ["string", "null"]},
                "level": {"type": ["string", "null"], "enum": ["✅", "❌", "❌❌", None]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "required": [],
        },
    },
    {
        "name": "get_call",
        "description": "Полная карточка одного звонка, включая транскрипт целиком. Использовать точечно, не для массового анализа.",
        "input_schema": {
            "type": "object",
            "properties": {"call_id": {"type": "integer"}},
            "required": ["call_id"],
        },
    },
    {
        "name": "compare_periods",
        "description": "Сравнение статистики между двумя периодами (например, эта неделя vs прошлая).",
        "input_schema": {
            "type": "object",
            "properties": {
                "period_a": _PERIOD_SCHEMA,
                "period_b": _PERIOD_SCHEMA,
                "manager_extension": {"type": ["string", "null"]},
            },
            "required": ["period_a", "period_b"],
        },
    },
    {
        "name": "get_criteria_breakdown",
        "description": "Разбивка по критериям оценки за период: сколько раз критерий был применим и сколько раз провален, отсортировано по доле провала.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": _PERIOD_SCHEMA,
                "manager_extension": {"type": ["string", "null"]},
            },
            "required": ["period"],
        },
    },
]
