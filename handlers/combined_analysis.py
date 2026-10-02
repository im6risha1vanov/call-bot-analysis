from __future__ import annotations
import json,os
from aiogram import Bot
from queue_runner import register,on_startup
from tools import resolve_actor
from combined import jobs
from combined.evaluation import VERSION


@on_startup
async def recover_combined(pool):
    await pool.execute("UPDATE combined_reports SET status='uncertain',error='restart during request' WHERE status='processing' AND response_text IS NULL")
    await pool.execute("UPDATE combined_report_delivery SET status='uncertain',error='restart during delivery' WHERE status='sending'")


@register('combined_analysis')
async def combined_analysis(pool,task):
    data=json.loads(task['input'])
    if data.get('version')!=VERSION or data.get('max_calls')!=2 or type(data.get('comparison_id')) is not int:
        raise ValueError('Invalid combined task')
    result=await jobs.run(pool,data['comparison_id'],task['client_id'])
    initiator=await pool.fetchval('SELECT initiator FROM combined_reports WHERE comparison_id=$1 AND version=$2 AND client_id=$3 ORDER BY ordinal LIMIT 1',data['comparison_id'],VERSION,task['client_id'])
    actor=await resolve_actor(pool,initiator)
    bot=Bot(os.environ['BOT_TOKEN'])
    try:await jobs.deliver(pool,bot,actor,data['comparison_id'])
    finally:await bot.session.close()
    return result
