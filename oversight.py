"""
Метрики надзора (Этап 5). Здесь нет ни одного обращения к модели — это
обычные запросы к базе: простой SQL быстрее, дешевле и надёжнее модели,
принимающей то же решение.

Модель подключается ровно один раз в сутки и только чтобы превратить уже
посчитанные здесь цифры в читаемый текст — см. handlers/oversight_report.py.
"""

from __future__ import annotations

from datetime import timedelta

import asyncpg

STUCK_AFTER_HOURS = 1


async def collect(pool: asyncpg.Pool, window_hours: int = 24) -> dict:
    # asyncpg ждёт для interval именно timedelta, строку '24 hours' он не
    # примет даже с явным ::interval.
    window = timedelta(hours=window_hours)

    by_type = await pool.fetch(
        """
        SELECT type, status, count(*) AS n
        FROM tasks
        WHERE updated_at >= now() - $1::interval
        GROUP BY type, status
        ORDER BY type, status
        """,
        window,
    )

    failures = await pool.fetch(
        """
        SELECT id, type, attempts, left(error, 300) AS error, updated_at
        FROM tasks
        WHERE status = 'failed' AND updated_at >= now() - $1::interval
        ORDER BY updated_at DESC
        LIMIT 10
        """,
        window,
    )

    stuck = await pool.fetch(
        """
        SELECT id, type, updated_at
        FROM tasks
        WHERE status = 'processing' AND updated_at < now() - $1::interval
        ORDER BY updated_at
        LIMIT 10
        """,
        timedelta(hours=STUCK_AFTER_HOURS),
    )

    queue = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE status = 'new') AS new,
               count(*) FILTER (WHERE status = 'processing') AS processing,
               count(*) FILTER (WHERE status = 'awaiting_approval') AS awaiting_approval
        FROM tasks
        """
    )

    limits = await pool.fetch(
        """
        SELECT task_type, reason, count(*) AS n
        FROM limit_hits
        WHERE created_at >= now() - $1::interval
        GROUP BY task_type, reason
        ORDER BY n DESC
        """,
        window,
    )

    spend_astra = await pool.fetchval(
        "SELECT coalesce(sum(spent_units), 0) FROM astra_daily_spend WHERE day >= current_date - 1"
    )
    spend_rop = await pool.fetchval(
        "SELECT coalesce(sum(spent_usd), 0) FROM rop_agent_daily_spend WHERE day >= current_date - 1"
    )

    counts: dict[str, dict[str, int]] = {}
    for r in by_type:
        counts.setdefault(r["type"], {})[r["status"]] = r["n"]

    return {
        "window_hours": window_hours,
        "tasks_by_type": counts,
        "failures": [
            {"task_id": r["id"], "type": r["type"], "attempts": r["attempts"],
             "error": r["error"], "at": r["updated_at"].isoformat()}
            for r in failures
        ],
        "stuck": [
            {"task_id": r["id"], "type": r["type"], "since": r["updated_at"].isoformat()}
            for r in stuck
        ],
        "queue": {
            "new": queue["new"],
            "processing": queue["processing"],
            "awaiting_approval": queue["awaiting_approval"],
        },
        "limit_hits": [
            {"type": r["task_type"], "reason": r["reason"], "count": r["n"]} for r in limits
        ],
        "spend": {
            "astra_units_2d": float(spend_astra or 0),
            "rop_agent_usd_2d": float(spend_rop or 0),
        },
    }


def is_quiet(metrics: dict, spend_usd_threshold: float) -> bool:
    """Сутки без происшествий: ничего не упало, ничего не висит, лимиты не
    срабатывали, расход агента РОПа в пределах порога. В этом случае отчёт —
    одна строка, и вызов модели не нужен вовсе: платить за фразу «всё хорошо»
    незачем."""
    return (
        not metrics["failures"]
        and not metrics["stuck"]
        and not metrics["limit_hits"]
        and metrics["spend"]["rop_agent_usd_2d"] <= spend_usd_threshold
    )


def render_quiet_line(metrics: dict) -> str:
    done = sum(s.get("done", 0) for s in metrics["tasks_by_type"].values())
    awaiting = metrics["queue"]["awaiting_approval"]
    tail = f", ждут подтверждения: {awaiting}" if awaiting else ""
    return (f"🟢 Надзор: за сутки всё штатно — задач выполнено {done}, сбоев нет, "
            f"очередь чистая{tail}.")
