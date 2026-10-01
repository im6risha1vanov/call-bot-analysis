from __future__ import annotations

import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .runtime import VERSION, shadow_enabled, settings


def fingerprint(transcript: str) -> str:
    return hashlib.sha256(transcript.encode()).hexdigest()


async def enqueue(pool, client_id: int, source: str, source_id: int, transcript: str):
    if not shadow_enabled() or not transcript.strip():
        return
    if source not in {"call", "training"}:
        raise ValueError("Unknown source")
    payload = {"source": source, "source_id": source_id, "sha256": fingerprint(transcript), "version": VERSION}
    await pool.execute(
        "INSERT INTO tasks(type,client_id,input,dedup_key) VALUES('evaluate_course',$1,$2::jsonb,$3) "
        "ON CONFLICT(type,dedup_key) DO NOTHING", client_id, json.dumps(payload),
        f"{client_id}:{source}:{source_id}:{VERSION}:{payload['sha256']}")


async def store_receipt(pool, evaluation_id, client, text, cost):
    """Receipt and spend are one transaction. The conditional update prevents double billing."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            changed = await connection.fetchval(
                "UPDATE methodology_evaluations SET response_text=$2,cost_units=$3,updated_at=now() "
                "WHERE id=$1 AND response_text IS NULL RETURNING id", evaluation_id, text, cost)
            if changed and cost:
                await connection.execute(
                    "INSERT INTO methodology_daily_spend(client_id,day,spent_units) VALUES($1,$2,$3) "
                    "ON CONFLICT(client_id,day) DO UPDATE SET spent_units=methodology_daily_spend.spent_units+EXCLUDED.spent_units",
                    client["id"], datetime.now(ZoneInfo(client["timezone"])).date(), cost)


async def over_daily_limit(pool, client):
    used = await pool.fetchval('SELECT spent_units FROM methodology_daily_spend WHERE client_id=$1 AND day=$2',
                               client['id'], datetime.now(ZoneInfo(client['timezone'])).date())
    return float(used or 0) >= settings()['daily_budget_units']
