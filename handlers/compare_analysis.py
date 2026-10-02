from __future__ import annotations
import json
import os
from aiogram import Bot
from queue_runner import register,on_startup
from tools import resolve_actor
from comparison.engine import run
from comparison.presentation import send_results


@on_startup
async def recover_comparisons(pool):
    await pool.execute("UPDATE analysis_comparison_stages SET status='uncertain',error='restart during request' WHERE status='processing' AND response_text IS NULL")
    await pool.execute("UPDATE analysis_comparison_delivery SET status='uncertain',error='restart during Telegram delivery' WHERE status='sending'")


@register('compare_analysis')
async def compare_analysis(pool,task):
    payload=json.loads(task['input'])
    if payload.get('max_calls')!=2 or type(payload.get('comparison_id')) is not int:
        raise ValueError('Invalid comparison task')
    run_id=payload['comparison_id']
    experiment=await pool.fetchrow('SELECT * FROM analysis_comparisons WHERE id=$1 AND client_id=$2',run_id,task['client_id'])
    if not experiment:
        raise ValueError('Comparison client mismatch')
    result=await run(pool,run_id)
    if result.get('busy'):
        from datetime import timedelta
        from queue_runner import RetryLater
        raise RetryLater(timedelta(seconds=20),'сравнение уже выполняется')
    actor=await resolve_actor(pool,experiment['initiator'])
    bot=Bot(os.environ['BOT_TOKEN'])
    try:
        await send_results(pool,bot,actor,run_id)
    finally:
        await bot.session.close()
    return result
