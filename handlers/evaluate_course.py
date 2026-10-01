"""Shadow evaluation cannot affect legacy verdicts, deliveries or assignments."""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from aiogram import Bot
import os

from queue_runner import RetryLater, on_startup, register
from methodology.evaluation import InvalidEvaluation, evaluate, render, score_prompt
from methodology.jobs import fingerprint, store_receipt, over_daily_limit
from methodology.runtime import VERSION, LEGACY_VERSION, shadow_enabled
from methodology import transport
from methodology.messages import split_plain

log = logging.getLogger(__name__)


@on_startup
async def restore_course(pool):
    await pool.execute("UPDATE methodology_evaluations SET status='uncertain',error='restart during evaluation',updated_at=now() WHERE status='processing' AND response_text IS NULL")
    await pool.execute("UPDATE training_sessions t SET status='failed',ended_at=now(),error='restart during course turn',updated_at=now() FROM methodology_training_context mc WHERE mc.session_id=t.id AND mc.turn_state IN ('pending','uncertain') AND t.status='active'")


async def _feedback(pool, evaluation, session):
    if evaluation["feedback_sent"] or not evaluation["result"]:
        return
    context = await pool.fetchrow("SELECT * FROM methodology_training_context WHERE session_id=$1", session["id"])
    if not context:
        return
    # Recheck binding before sending to a stored Telegram identity.
    bound = await pool.fetchval("SELECT EXISTS(SELECT 1 FROM employees WHERE client_id=$1 AND extension=$2 AND telegram_user_id=$3)",
                                session["client_id"], session["extension"], context["telegram_user_id"])
    if not bound:
        return
    bot = Bot(token=os.environ["TRAIN_BOT_TOKEN"])
    try:
        text = "Разбор тренировки\n\n" + render(json.loads(evaluation["result"]), detailed=True)
        for part, chunk in enumerate(split_plain(text)):
            parts_sent = evaluation['feedback_parts_sent']
            if part >= parts_sent:
                await bot.send_message(context["telegram_user_id"], chunk, parse_mode=None)
                await pool.execute('UPDATE methodology_evaluations SET feedback_parts_sent=$2 WHERE id=$1', evaluation['id'], part + 1)
        await pool.execute("UPDATE methodology_evaluations SET feedback_sent=true WHERE id=$1", evaluation["id"])
    finally:
        await bot.session.close()


@register("evaluate_course")
async def evaluate_course(pool, task):
    if not shadow_enabled():
        raise RetryLater(timedelta(minutes=30), 'методика курса выключена')
    payload = json.loads(task["input"])
    if payload.get("version") != VERSION or payload.get("source") not in {"call", "training"}:
        raise ValueError("Invalid methodology task")
    source = payload["source"]
    table = "calls" if source == "call" else "training_sessions"
    item = await pool.fetchrow(f"SELECT * FROM {table} WHERE id=$1 AND client_id=$2", payload["source_id"], task["client_id"])
    if not item:
        return {"skipped": "source unavailable"}
    client = await pool.fetchrow("SELECT * FROM clients WHERE id=$1", task["client_id"])
    if not client["processing_enabled"]:
        raise RetryLater(timedelta(minutes=30), "обработка клиента отключена")
    if source == "call":
        transcript = item["transcript"] or ""
        old = await pool.fetchrow("SELECT score,level FROM astra_analysis WHERE call_id=$1", item["id"])
        legacy = {"version": LEGACY_VERSION, **dict(old or {})}
        context = {"call_started_at": item["call_started_at"], "timezone": client["timezone"]}
    else:
        turns = json.loads(item["transcript"])
        transcript = "\n".join(("Менеджер: " if t["role"] == "manager" else "Клиент: ") + t["text"] for t in turns)
        legacy = {"version": LEGACY_VERSION, "score": item["score"], "level": item["level"]}
        training_context = await pool.fetchrow("SELECT scenario FROM methodology_training_context WHERE session_id=$1", item["id"])
        context = {"training": True, "scenario": json.loads(training_context["scenario"]) if training_context else None,
                   "mode": item['mode'], "exercises": json.loads(item['drill_state']).get('results', []) if item['mode']=='drill' else []}
    digest = fingerprint(transcript)
    if digest != payload["sha256"]:
        return {"skipped": "transcript changed; new version must be enqueued"}
    column = "call_id" if source == "call" else "training_session_id"
    existing = await pool.fetchrow(f"SELECT * FROM methodology_evaluations WHERE {column}=$1 AND version=$2 AND transcript_sha256=$3", item["id"], VERSION, digest)
    if existing:
        evaluation_id = existing["id"]
        if existing["status"] == "complete":
            if source == "training":
                await _feedback(pool, existing, item)
            return {"evaluation_id": evaluation_id, "cached": True}
        if existing["response_text"] is None:
            return {"evaluation_id": evaluation_id, "status": existing["status"], "needs_review": True}
        text = existing["response_text"]
    else:
        if await over_daily_limit(pool, client):
            raise RetryLater(timedelta(minutes=30), "дневной лимит клиента исчерпан")
        inserted = await pool.fetchval(
            f"INSERT INTO methodology_evaluations(client_id,{column},version,transcript_sha256,legacy_result,status) "
            "VALUES($1,$2,$3,$4,$5::jsonb,'processing') ON CONFLICT DO NOTHING RETURNING id",
            client["id"], item["id"], VERSION, digest, json.dumps(legacy))
        if not inserted:
            raise RetryLater(timedelta(seconds=10), "оценка уже занята")
        evaluation_id = inserted
        try:
            text, cost = await transport.request(score_prompt(), transcript, context)
        except transport.ProviderFailure as exc:
            status = "failed" if exc.status in {400, 401, 402, 403, 404, 429} else "uncertain"
            await pool.execute("UPDATE methodology_evaluations SET status=$2,error=$3,updated_at=now() WHERE id=$1",
                               evaluation_id, status, str(exc))
            return {"evaluation_id": evaluation_id, "status": status, "needs_review": True}
        # A DB failure here leaves processing; subsequent execution does not repeat the paid request.
        await store_receipt(pool, evaluation_id, client, text, cost)
    try:
        raw = json.loads(text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip(),
                         parse_constant=lambda value: (_ for _ in ()).throw(InvalidEvaluation("Nonfinite JSON number")))
        result = evaluate(raw, transcript)
        if source=='training' and item['mode']=='drill' and training_context and len(result['exercise_results']) != len(context['exercises']):
            raise InvalidEvaluation('Every answered course exercise requires an evaluation')
    except (InvalidEvaluation, ValueError, TypeError) as exc:
        await pool.execute("UPDATE methodology_evaluations SET status='failed',error=$2,updated_at=now() WHERE id=$1", evaluation_id, type(exc).__name__)
        return {"evaluation_id": evaluation_id, "status": "failed", "needs_review": True}
    await pool.execute("UPDATE methodology_evaluations SET status='complete',result=$2::jsonb,error=NULL,updated_at=now() WHERE id=$1", evaluation_id, json.dumps(result, ensure_ascii=False))
    if source == "training":
        current = await pool.fetchrow("SELECT * FROM methodology_evaluations WHERE id=$1", evaluation_id)
        await _feedback(pool, current, item)
    return {"evaluation_id": evaluation_id, "version": VERSION, "shadow": True}
