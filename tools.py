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
import logging
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from typing import Literal
from zoneinfo import ZoneInfo

import asyncpg

from reports import fmt_phone

log = logging.getLogger("tools")


class Forbidden(Exception):
    """Действующему лицу не положен доступ к запрошенным данным."""


@dataclass(frozen=True)
class Actor:
    """Тот, от чьего имени вызывается инструмент. client_id, role и extension
    получены из employees по telegram_user_id — не из параметров, которые
    прислала модель."""
    telegram_user_id: int
    client_id: int
    # head — руководитель отдела.
    # owner — владелец системы: получает всё то же, что руководитель, плюс
    #   отдельный технический отчёт о работе самой системы. Права одинаковые.
    # manager — менеджер: код сужает его до собственных звонков.
    role: Literal["head", "owner", "manager"]
    extension: str | None  # у head и owner добавочного нет


async def resolve_actor(pool: asyncpg.Pool, telegram_user_id: int) -> Actor | None:
    row = await pool.fetchrow(
        "SELECT client_id, role, extension FROM employees WHERE telegram_user_id=$1",
        telegram_user_id,
    )
    if row is None:
        return None
    return Actor(telegram_user_id=telegram_user_id, client_id=row["client_id"],
                 role=row["role"], extension=row["extension"])


def is_privileged(actor: Actor) -> bool:
    """Полный доступ к данным отдела: руководитель и владелец системы. Всё, что
    не менеджер, — а менеджера код жёстко сужает до его собственных звонков."""
    return actor.role != "manager"


async def report_chat_ids(pool: asyncpg.Pool, client_id: int) -> list[int]:
    """Кому уходят отчёты по отделу: разборы звонков, дайджесты, планёрка,
    сводки, сообщения о сбоях. Это руководители И владелец системы — владелец
    получает всё то же, что руководитель, плюс отдельный технический отчёт.

    Раньше здесь стоял fetchrow(... role=head) — то есть одному, а какому
    именно, решал порядок строк; с появлением второго получателя это значило бы
    «кому повезёт»."""
    rows = await pool.fetch(
        "SELECT telegram_user_id FROM employees "
        "WHERE client_id=$1 AND role IN ('head', 'owner') AND telegram_user_id IS NOT NULL "
        "ORDER BY id",
        client_id,
    )
    return [r["telegram_user_id"] for r in rows]


async def owner_chat_ids(pool: asyncpg.Pool, client_id: int) -> list[int]:
    """Владельцы системы — получатели технического отчёта о её работе."""
    rows = await pool.fetch(
        "SELECT telegram_user_id FROM employees "
        "WHERE client_id=$1 AND role='owner' AND telegram_user_id IS NOT NULL ORDER BY id",
        client_id,
    )
    return [r["telegram_user_id"] for r in rows]


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


def _local_iso(dt, tz_name: str) -> str | None:
    """Время наружу — всегда в часовом поясе клиента. В базе timestamptz, и
    asyncpg отдаёт его в UTC: без перевода агент РОПа показывал бы руководителю
    время сервера вместо московского."""
    if dt is None:
        return None
    return dt.astimezone(ZoneInfo(tz_name)).isoformat(timespec="minutes")


# Английские идентификаторы из базы модель видеть может, а в отчёте они
# выглядеть не должны — переводим здесь, на границе выдачи данных, а не надеясь
# на аккуратность модели. Названия сценариев повторяют SCENARIOS из
# training_simulator: импортировать его сюда нельзя, он сам импортирует tools.
SCENARIO_LABELS = {
    "brush_off_handled": "отговорки",
    "gatekeeper_passed": "не тот человек",
    "implication_questions": "поверхностные вопросы",
    "explicit_need": "не проговорена потребность",
    "decision_influence": "скрытое влияние на решение",
    "no_early_pitch": "ранняя презентация",
}
MODE_LABELS = {"dialog": "разговор целиком", "drill": "отработка возражений"}
SESSION_STATUS_LABELS = {
    "active": "идёт", "completed": "завершена",
    "abandoned": "прервана", "failed": "сбой",
}


def _drill_summary(drill_state_json) -> dict | None:
    """Сводка по отработке возражений: сколько зачтено и какие именно не
    зачтены — агенту РОПа этого хватает, полные ответы менеджера ему не нужны."""
    if not drill_state_json:
        return None
    results = (json.loads(drill_state_json) or {}).get("results") or []
    return {
        "passed": sum(1 for r in results if r.get("passed")),
        "total": len(results),
        "failed_objections": [r["objection"] for r in results if not r.get("passed")],
    }


def _level_counts(rows) -> dict[str, int]:
    counts = {"✅": 0, "⚠️": 0, "❌": 0}
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

async def find_calls(pool: asyncpg.Pool, actor: Actor, period: dict | None = None,
                      manager_extension: str | None = None, level: str | None = None,
                      limit: int = 20) -> list[dict]:
    extension = _scope_extension(actor, manager_extension)
    client = await _require_client(pool, actor.client_id)
    start, end = (None, None)
    if period:
        start, end = _period_bounds(client["timezone"], period)
    limit = min(int(limit), 100)

    rows = await pool.fetch(
        """
        SELECT c.id, c.extension, c.client_number, c.call_started_at, c.duration_seconds,
               a.level, a.short_report
        FROM astra_analysis a JOIN calls c ON c.id = a.call_id
        WHERE c.client_id = $1 AND a.status = 'analyzed'
          AND ($2::text IS NULL OR c.extension = $2)
          AND ($3::timestamptz IS NULL OR a.updated_at >= $3)
          AND ($4::timestamptz IS NULL OR a.updated_at < $4)
          AND ($5::text IS NULL OR a.level = $5)
        -- NULLS LAST: у части звонков Манго не отдала время начала, а при DESC
        -- пустые идут первыми — агент получал бы их как «самые свежие».
        ORDER BY c.call_started_at DESC NULLS LAST
        LIMIT $6
        """,
        actor.client_id, extension, start, end, level, limit,
    )
    return [
        {
            # Звонок опознают по номеру клиента: сотрудник ищет по нему в Манго.
            # Внутренний номер записи (call_id) наружу не показываем — в
            # интерфейсе Манго его нет, найти по нему звонок нельзя.
            "client_phone": fmt_phone(r["client_number"]),
            "call_id": r["id"],
            "manager_extension": r["extension"],
            "started_at": _local_iso(r["call_started_at"], client["timezone"]),
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
        SELECT c.id, c.client_id, c.extension, c.client_number, c.call_started_at, c.duration_seconds,
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

    client = await _require_client(pool, actor.client_id)
    return {
        "client_phone": fmt_phone(row["client_number"]),
        "call_id": row["id"],
        "manager_extension": row["extension"],
        "started_at": _local_iso(row["call_started_at"], client["timezone"]),
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


# --------------------------------------------------------- successful evidence

async def get_successful_evidence(pool: asyncpg.Pool, actor: Actor, period: dict,
                                   criterion: str | None = None,
                                   manager_extension: str | None = None) -> list[dict]:
    """Цитаты из звонков, где критерий был пройден — сырьё для предложений по
    скрипту (Этап 3, "удачные отклонения"). Само по себе не решает, что
    отклонение удачное и повторяющееся у разных менеджеров — это суждение
    оставлено агенту (сравнить цитаты между менеджерами), а не коду: искать
    "тот же приём" в свободном тексте кода ненадёжно."""
    extension = _scope_extension(actor, manager_extension)
    client = await _require_client(pool, actor.client_id)
    start, end = _period_bounds(client["timezone"], period)

    rows = await pool.fetch(
        """
        SELECT c.extension, a.analysis FROM astra_analysis a JOIN calls c ON c.id = a.call_id
        WHERE c.client_id = $1 AND a.status = 'analyzed'
          AND a.updated_at >= $2 AND a.updated_at < $3
          AND ($4::text IS NULL OR c.extension = $4)
          AND a.analysis IS NOT NULL
        """,
        actor.client_id, start, end, extension,
    )
    out = []
    for r in rows:
        for row in (json.loads(r["analysis"]).get("rows") or []):
            if not (row.get("applicable") and row.get("passed") and row.get("evidence")):
                continue
            if criterion and row["key"] != criterion:
                continue
            out.append({"manager_extension": r["extension"], "criterion": row["title"], "evidence": row["evidence"]})
    return out


# ----------------------------------------------------------- lead diagnosis

async def get_lead_diagnosis_signal(pool: asyncpg.Pool, actor: Actor, period: dict,
                                     manager_extension: str | None = None) -> dict:
    """Доля звонков с диагнозом "база" среди тех, где диагноз вообще
    посчитан. ВАЖНО: diagnosis.type считается только из detailed_report —
    подробный разбор строится лениво, по клику "Подробный разбор" в
    Telegram, а не для каждого звонка (так решили специально, чтобы втрое
    снизить расход на API). Поэтому sample здесь — не все звонки периода, а
    только те, что кто-то уже открыл подробно. Агент обязан явно предупредить
    об этом смещении выборки, а не выдавать долю как статистику по всем звонкам."""
    extension = _scope_extension(actor, manager_extension)
    client = await _require_client(pool, actor.client_id)
    start, end = _period_bounds(client["timezone"], period)

    rows = await pool.fetch(
        """
        SELECT a.detailed_report FROM astra_analysis a JOIN calls c ON c.id = a.call_id
        WHERE c.client_id = $1 AND a.status = 'analyzed'
          AND a.updated_at >= $2 AND a.updated_at < $3
          AND ($4::text IS NULL OR c.extension = $4)
          AND a.detailed_report IS NOT NULL
        """,
        actor.client_id, start, end, extension,
    )
    types: dict[str, int] = {}
    for r in rows:
        t = (json.loads(r["detailed_report"]).get("diagnosis") or {}).get("type")
        if t:
            types[t] = types.get(t, 0) + 1
    reviewed = len(rows)
    return {
        "period": period, "extension": extension,
        "reviewed_calls": reviewed,
        "diagnosis_counts": types,
        "base_share": round(types.get("база", 0) / reviewed, 3) if reviewed else None,
        "sample_caveat": ("Это не все звонки периода, а только те, где кто-то запросил "
                           "«Подробный разбор» — подробный разбор считается не для каждого "
                           "звонка автоматически. При маленьком reviewed_calls вывод ненадёжен."),
    }


async def get_training_history(pool: asyncpg.Pool, actor: Actor, manager_extension: str | None = None,
                                limit: int = 10) -> list[dict]:
    """Видимость Этапа 4 (тренажёр) для агента РОПа: кто тренировался, по
    какой теме/сценарию, с каким результатом. Оценка тренировки — той же
    линейкой (score_call/compute_level), что и реальные звонки, поэтому level
    здесь сравним с level в get_stats/find_calls."""
    extension = _scope_extension(actor, manager_extension)
    client = await _require_client(pool, actor.client_id)
    limit = min(int(limit), 50)
    rows = await pool.fetch(
        """
        SELECT id, extension, scenario_kind, topic, status, level, score, turns_count,
               mode, drill_state, started_at, ended_at
        FROM training_sessions
        WHERE client_id = $1 AND ($2::text IS NULL OR extension = $2)
          -- Пробные прогоны руководителя (он смотрит, годится ли тренажёр)
          -- в статистику отдела не идут: иначе агент считал бы их работой
          -- менеджеров.
          AND is_test = false
        ORDER BY started_at DESC LIMIT $3
        """,
        actor.client_id, extension, limit,
    )
    return [
        {
            "session_id": r["id"], "manager_extension": r["extension"],
            "scenario": SCENARIO_LABELS.get(r["scenario_kind"], r["scenario_kind"]),
            "topic": r["topic"],
            "status": SESSION_STATUS_LABELS.get(r["status"], r["status"]),
            "level": r["level"], "score": r["score"],
            "turns": r["turns_count"],
            # dialog — разговор целиком, оценён теми же критериями, что и реальные
            # звонки (level сравним с get_stats). drill — отработка возражений
            # поштучно: level не ставится, score — доля зачтённых ответов.
            "mode": MODE_LABELS.get(r["mode"], r["mode"]),
            "objections_handled": _drill_summary(r["drill_state"]) if r["mode"] == "drill" else None,
            "started_at": _local_iso(r["started_at"], client["timezone"]),
            "ended_at": _local_iso(r["ended_at"], client["timezone"]),
        }
        for r in rows
    ]


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
        "description": "Сводная статистика по звонкам за период: сколько всего, разбивка по уровням ✅/⚠️/❌, доля успеха.",
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
                "level": {"type": ["string", "null"], "enum": ["✅", "⚠️", "❌", None]},
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
    {
        "name": "get_successful_evidence",
        "description": ("Цитаты из звонков, где конкретный критерий был пройден — сырьё для идей по скрипту. "
                         "Сам инструмент не решает, какой приём удачный и повторяющийся — сравнивай цитаты "
                         "между разными manager_extension самостоятельно, прежде чем предлагать правку сценария, "
                         "и не предлагай правку по одной цитате от одного менеджера."),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": _PERIOD_SCHEMA,
                "criterion": {"type": ["string", "null"],
                              "description": "Ключ критерия (например insight, call_reason); null — все критерии"},
                "manager_extension": {"type": ["string", "null"]},
            },
            "required": ["period"],
        },
    },
    {
        "name": "get_lead_diagnosis_signal",
        "description": ("Доля звонков с диагнозом «база» (проблема в качестве лидов, не в менеджере) за период. "
                         "ВАЖНО: считается только по звонкам, где кто-то уже открывал «Подробный разбор» — это "
                         "не полная выборка периода. Всегда сообщай reviewed_calls и sample_caveat из ответа, "
                         "если используешь этот сигнал в выводах, и не утверждай долю как относящуюся ко всем "
                         "звонкам периода."),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": _PERIOD_SCHEMA,
                "manager_extension": {"type": ["string", "null"]},
            },
            "required": ["period"],
        },
    },
    {
        "name": "get_training_history",
        "description": ("История тренировок в тренажёре возражений: кто тренировался, по какому сценарию/теме, "
                         "с каким уровнем (той же линейкой ✅/⚠️/❌, что и реальные звонки) и был ли сдвиг. "
                         "status='active' — тренировка ещё идёт, у неё пока нет level."),
        "input_schema": {
            "type": "object",
            "properties": {
                "manager_extension": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": [],
        },
    },
]
