from __future__ import annotations

import json
import os
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from combined import evaluation
from comparison import transport
from comparison.engine import parse
from comparison.registry import sha
from comparison.store import decode
from methodology import runtime
from methodology.evaluation import score_prompt
from queue_runner import RetryLater
from tools import Forbidden, get_call

LIMIT = float(os.getenv('MAX_COST_PER_CHAT_UNITS', '2000000'))


def enabled():
    try:
        return runtime.shadow_enabled() and json.loads((Path(__file__).parent/'config.json').read_text()).get('enabled') is True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


async def installed(pool):
    return bool(await pool.fetchval("SELECT to_regclass('automatic_call_spend') IS NOT NULL"))


async def over_budget(pool, client):
    """One existing client ceiling, including both automatic alternatives."""
    day = datetime.now(ZoneInfo(client['timezone'])).date()
    spent = await pool.fetchval("""SELECT coalesce(sum(spent_units),0) FROM (
        SELECT spent_units FROM astra_daily_spend WHERE client_id=$1 AND day=$2
        UNION ALL SELECT spent_units FROM methodology_daily_spend WHERE client_id=$1 AND day=$2
        UNION ALL SELECT spent_units FROM automatic_call_spend WHERE client_id=$1 AND day=$2
    ) s""", client['id'], day)
    return float(spent) >= LIMIT


async def enqueue(pool, client_id, call_id, transcript, metadata):
    if not enabled() or not transcript.strip():
        return False
    manifest = evaluation.verify()
    digest = sha(transcript)
    config, catalog = runtime.settings(), runtime.approved_catalog()
    prompt = score_prompt()+'\n\n'+(evaluation.ROOT/'prompt.md').read_text()
    settings = {**transport.settings(), 'max_output_tokens': 8000}
    async with (pool.acquire() if hasattr(pool,'acquire') else nullcontext(pool)) as conn, conn.transaction():
        await conn.execute("INSERT INTO automatic_call_reports(call_id,version,client_id,transcript_sha256,transcript,metadata,package_sha256,prompt_sha256,prompt,settings,config,catalog) VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10::jsonb,$11::jsonb,$12::jsonb) ON CONFLICT DO NOTHING",
                           call_id, evaluation.VERSION, client_id, digest, transcript, json.dumps(metadata),
                           manifest['sha256'], sha(prompt), prompt, json.dumps(settings), json.dumps(config), json.dumps(catalog))
        task = await conn.fetchval("INSERT INTO tasks(type,client_id,input,dedup_key) VALUES('automatic_combined_call',$1,$2::jsonb,$3) ON CONFLICT(type,dedup_key) DO NOTHING RETURNING id",
                                  client_id, json.dumps({'call_id':call_id,'version':evaluation.VERSION,'sha256':digest}),
                                  f'{client_id}:{call_id}:{evaluation.VERSION}:{digest}')
    return task is not None


async def get(pool, actor, call_id):
    if actor is None or actor.role not in {'manager','head','owner'}:
        raise Forbidden('Доступ к разбору закрыт.')
    call = await get_call(pool, actor, call_id)
    row = await pool.fetchrow('SELECT * FROM automatic_call_reports WHERE call_id=$1 AND client_id=$2 AND version=$3 AND transcript_sha256=$4',
                              call_id, actor.client_id, evaluation.VERSION, sha(call['transcript'] or ''))
    return row


async def receipt(pool, row, client, response, cost, usage):
    async with pool.acquire() as conn, conn.transaction():
        changed = await conn.fetchval("UPDATE automatic_call_reports SET status='received',response_text=$4,cost_units=$5,usage=$6::jsonb WHERE call_id=$1 AND version=$2 AND transcript_sha256=$3 AND response_text IS NULL RETURNING call_id",
                                      row['call_id'], row['version'], row['transcript_sha256'], response, cost, json.dumps(usage))
        if changed and cost:
            await conn.execute("INSERT INTO automatic_call_spend VALUES($1,$2,$3) ON CONFLICT(client_id,day) DO UPDATE SET spent_units=automatic_call_spend.spent_units+EXCLUDED.spent_units",
                               client['id'], datetime.now(ZoneInfo(client['timezone'])).date(), cost)


async def run(pool, task):
    data = decode(task['input'])
    if not isinstance(data,dict) or type(data.get('call_id')) is not int or data.get('version')!=evaluation.VERSION or not isinstance(data.get('sha256'),str):
        raise ValueError('Invalid automatic combined task')
    async with pool.acquire() as conn:
        key = f'automatic-combined:{data["call_id"]}:{evaluation.VERSION}'
        if not await conn.fetchval('SELECT pg_try_advisory_lock(hashtextextended($1,0))',key):
            raise RetryLater(timedelta(seconds=20),'объединённый разбор уже выполняется')
        try:
            row = await conn.fetchrow('SELECT * FROM automatic_call_reports WHERE call_id=$1 AND version=$2 AND transcript_sha256=$3 AND client_id=$4',data['call_id'],evaluation.VERSION,data['sha256'],task['client_id'])
            if not row:
                raise ValueError('Automatic snapshot unavailable')
            client = await conn.fetchrow('SELECT * FROM clients WHERE id=$1',row['client_id'])
            current = await conn.fetchrow('SELECT transcript,status FROM calls WHERE id=$1 AND client_id=$2',row['call_id'],row['client_id'])
            if not current or current['status']!='analyzed' or sha(current['transcript'] or '')!=row['transcript_sha256']:
                return {'skipped':'call or transcript changed'}
            if row['status']=='complete':
                return {'call_id':row['call_id'],'status':'complete','cached':True}
            if row['response_text'] is None and row['status']!='pending':
                if row['status']=='processing':
                    await conn.execute("UPDATE automatic_call_reports SET status='uncertain',error='unknown interrupted request' WHERE call_id=$1 AND version=$2 AND transcript_sha256=$3",row['call_id'],row['version'],row['transcript_sha256'])
                return {'call_id':row['call_id'],'status':'needs_review'}
            if sha(row['transcript'])!=row['transcript_sha256'] or sha(row['prompt'])!=row['prompt_sha256'] or evaluation.verify()['sha256']!=row['package_sha256']:
                raise ValueError('Pinned automatic package changed')
            response = row['response_text']
            if response is None:
                if not enabled() or not client['processing_enabled']:
                    raise RetryLater(timedelta(minutes=30),'автоматическая обработка отключена')
                if await over_budget(conn,client):
                    raise RetryLater(timedelta(minutes=30),'дневной лимит клиента исчерпан')
                await conn.execute("UPDATE automatic_call_reports SET status='processing' WHERE call_id=$1 AND version=$2 AND transcript_sha256=$3",row['call_id'],row['version'],row['transcript_sha256'])
                user = 'КОНТЕКСТ (не заменяет цитаты):\n'+json.dumps(decode(row['metadata']),ensure_ascii=False)+'\n\nТРАНСКРИПТ:\n'+row['transcript']
                try:
                    response,cost,usage = await transport.request(row['prompt'],user,decode(row['settings']))
                except transport.RequestFailure as exc:
                    state = 'failed' if exc.status in {400,401,402,403,404,429} else 'uncertain'
                    await conn.execute('UPDATE automatic_call_reports SET status=$4,error=$5,completed_at=now() WHERE call_id=$1 AND version=$2 AND transcript_sha256=$3',row['call_id'],row['version'],row['transcript_sha256'],state,str(exc))
                    return {'call_id':row['call_id'],'status':state}
                await receipt(pool,row,client,response,cost,usage)
            try:
                result = evaluation.evaluate(parse(response),row['transcript'],config=decode(row['config']),catalog=decode(row['catalog']))
            except (ValueError,TypeError,KeyError,AttributeError) as exc:
                await conn.execute("UPDATE automatic_call_reports SET status='failed',error=$4,completed_at=now() WHERE call_id=$1 AND version=$2 AND transcript_sha256=$3",row['call_id'],row['version'],row['transcript_sha256'],type(exc).__name__)
                return {'call_id':row['call_id'],'status':'failed'}
            await conn.execute("UPDATE automatic_call_reports SET status='complete',result=$4::jsonb,error=NULL,completed_at=now() WHERE call_id=$1 AND version=$2 AND transcript_sha256=$3",row['call_id'],row['version'],row['transcript_sha256'],json.dumps(result,ensure_ascii=False))
            return {'call_id':row['call_id'],'status':'complete'}
        finally:
            await conn.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))',key)
