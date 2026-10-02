from __future__ import annotations
import json
from datetime import timedelta
from aiogram.types import BufferedInputFile
from comparison.engine import parse
from comparison.registry import COURSE,sha
from comparison.store import authorized,decode,get as get_comparison
from comparison import transport
from tools import Forbidden,resolve_actor
from queue_runner import RetryLater
from . import evaluation,render


async def request(pool,actor,comparison_id=None):
    authorized(actor)
    if comparison_id is None:
        comparison_id=await pool.fetchval('SELECT id FROM analysis_comparisons WHERE client_id=$1 AND initiator=$2 ORDER BY id DESC LIMIT 1',actor.client_id,actor.telegram_user_id)
    if comparison_id is None:
        raise ValueError('Сначала выберите звонки через /compare_analysis.')
    source=await get_comparison(pool,actor,comparison_id,initiator=True)
    if source['status'] not in {'ready','partial'}:
        raise ValueError('Сначала дождитесь завершения исходного сравнения.')
    manifest=evaluation.verify()
    pinned=decode(source['packages'])[COURSE]
    instructions=pinned['score_prompt']+'\n\n'+(evaluation.ROOT/'prompt.md').read_text()
    settings=decode(source['settings'])
    # One extraction+narrative instead of three paid stages; reserve room for valid JSON.
    settings={**settings,'max_output_tokens':8000}
    async with pool.acquire() as conn,conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',f'combined-create:{comparison_id}:{evaluation.VERSION}')
        calls=await conn.fetch('SELECT * FROM analysis_comparison_calls WHERE comparison_id=$1 ORDER BY ordinal',comparison_id)
        if not 1<=len(calls)<=2:
            raise ValueError('Для пробы доступны один или два звонка.')
        created=False
        for call in calls:
            inserted=await conn.fetchval("INSERT INTO combined_reports(comparison_id,ordinal,version,client_id,initiator,package_sha256,transcript_sha256,prompt,settings,config,catalog) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,$11::jsonb) ON CONFLICT DO NOTHING RETURNING ordinal",
                                         comparison_id,call['ordinal'],evaluation.VERSION,actor.client_id,actor.telegram_user_id,
                                         manifest['sha256'],call['transcript_sha256'],instructions,json.dumps(settings),json.dumps(pinned['config']),json.dumps(pinned['catalog']))
            created=created or inserted is not None
        if created:
            await conn.execute("INSERT INTO tasks(type,client_id,input,dedup_key) VALUES('combined_analysis',$1,$2::jsonb,$3) ON CONFLICT DO NOTHING",actor.client_id,json.dumps({'comparison_id':comparison_id,'version':evaluation.VERSION,'max_calls':2}),f'combined:{comparison_id}:{evaluation.VERSION}')
        return comparison_id,created


async def get(pool,actor,comparison_id,ordinal):
    await get_comparison(pool,actor,comparison_id)
    row=await pool.fetchrow('SELECT r.*,c.transcript,c.metadata FROM combined_reports r JOIN analysis_comparison_calls c USING(comparison_id,ordinal) WHERE r.comparison_id=$1 AND r.ordinal=$2 AND r.version=$3 AND r.client_id=$4',comparison_id,ordinal,evaluation.VERSION,actor.client_id)
    if not row:
        raise Forbidden('Объединённый разбор недоступен.')
    return row


async def run(pool,comparison_id,client_id):
    async with pool.acquire() as conn:
        key=f'combined-run:{comparison_id}:{evaluation.VERSION}'
        if not await conn.fetchval('SELECT pg_try_advisory_lock(hashtextextended($1,0))',key):
            raise RetryLater(timedelta(seconds=20),'объединённый разбор уже выполняется')
        try:
            rows=await conn.fetch('SELECT * FROM combined_reports WHERE comparison_id=$1 AND version=$2 AND client_id=$3 ORDER BY ordinal',comparison_id,evaluation.VERSION,client_id)
            if not 1<=len(rows)<=2:
                raise ValueError('Invalid combined report task')
            for row in rows:
                actor=await resolve_actor(conn,row['initiator']);authorized(actor)
                if actor.client_id!=client_id:
                    raise Forbidden('Привязка инициатора изменилась.')
                call=await conn.fetchrow('SELECT * FROM analysis_comparison_calls WHERE comparison_id=$1 AND ordinal=$2',comparison_id,row['ordinal'])
                if evaluation.verify()['sha256']!=row['package_sha256'] or sha(call['transcript'])!=row['transcript_sha256']:
                    raise ValueError('Pinned combined package or transcript changed')
                if row['status']=='complete':continue
                if row['status'] in {'processing','failed','uncertain'} and row['response_text'] is None:
                    if row['status']=='processing':
                        await conn.execute("UPDATE combined_reports SET status='uncertain',error='resume during unknown request' WHERE comparison_id=$1 AND ordinal=$2 AND version=$3",comparison_id,row['ordinal'],evaluation.VERSION)
                    continue
                try:
                    response=row['response_text']
                    if response is None:
                        await conn.execute("UPDATE combined_reports SET status='processing' WHERE comparison_id=$1 AND ordinal=$2 AND version=$3",comparison_id,row['ordinal'],evaluation.VERSION)
                        user='КОНТЕКСТ (не заменяет цитаты):\n'+json.dumps(decode(call['metadata']),ensure_ascii=False)+'\n\nТРАНСКРИПТ:\n'+call['transcript']
                        response,cost,usage=await transport.request(row['prompt'],user,decode(row['settings']))
                        await conn.execute("UPDATE combined_reports SET status='received',response_text=$4,cost_units=$5,usage=$6::jsonb WHERE comparison_id=$1 AND ordinal=$2 AND version=$3",comparison_id,row['ordinal'],evaluation.VERSION,response,cost,json.dumps(usage))
                    result=evaluation.evaluate(parse(response),call['transcript'],config=decode(row['config']),catalog=decode(row['catalog']))
                    await conn.execute("UPDATE combined_reports SET status='complete',result=$4::jsonb,error=NULL,completed_at=now() WHERE comparison_id=$1 AND ordinal=$2 AND version=$3",comparison_id,row['ordinal'],evaluation.VERSION,json.dumps(result,ensure_ascii=False))
                except transport.RequestFailure as exc:
                    state='failed' if exc.status in {400,401,402,403,404,429} else 'uncertain'
                    await conn.execute('UPDATE combined_reports SET status=$4,error=$5,completed_at=now() WHERE comparison_id=$1 AND ordinal=$2 AND version=$3',comparison_id,row['ordinal'],evaluation.VERSION,state,str(exc))
                except (ValueError,TypeError,KeyError,AttributeError) as exc:
                    await conn.execute("UPDATE combined_reports SET status='failed',error=$4,completed_at=now() WHERE comparison_id=$1 AND ordinal=$2 AND version=$3",comparison_id,row['ordinal'],evaluation.VERSION,type(exc).__name__)
            return {'comparison_id':comparison_id,'version':evaluation.VERSION}
        finally:
            await conn.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))',key)


async def deliver(pool,bot,actor,comparison_id,*,reopen=False):
    authorized(actor)
    await get_comparison(pool,actor,comparison_id)
    chat=await bot.get_chat(actor.telegram_user_id)
    if chat.type!='private':raise Forbidden('Отчёты доступны в личном чате.')
    async with pool.acquire() as conn:
        key=f'combined-deliver:{comparison_id}:{actor.telegram_user_id}'
        if not await conn.fetchval('SELECT pg_try_advisory_lock(hashtextextended($1,0))',key):return
        try:
            calls=await conn.fetch('SELECT ordinal FROM combined_reports WHERE comparison_id=$1 AND version=$2 AND client_id=$3 ORDER BY ordinal',comparison_id,evaluation.VERSION,actor.client_id)
            for call in calls:
                row=await get(conn,actor,comparison_id,call['ordinal'])
                if row['status'] in {'pending','processing','received'}:continue
                saved_preview=bool(row['result'] and decode(row['result']).get('data_origin')=='saved_analyses_preview')
                if row['status']=='complete' or saved_preview:
                    chunks=render.cards(decode(row['result']),decode(row['metadata']))
                else:
                    chunks=[f"Объединённый разбор звонка №{decode(row['metadata'])['call_id']} не получен. Статус: {row['status']}. Платный запрос автоматически не повторяется."]
                for part,chunk in enumerate(chunks):
                    if not reopen:
                        old=await conn.fetchval('SELECT status FROM combined_report_delivery WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND recipient=$4 AND part=$5',comparison_id,call['ordinal'],evaluation.VERSION,actor.telegram_user_id,part)
                        if old in {'sent','sending','uncertain'}:continue
                    await conn.execute("INSERT INTO combined_report_delivery(comparison_id,ordinal,version,recipient,part,status) VALUES($1,$2,$3,$4,$5,'sending') ON CONFLICT(comparison_id,ordinal,version,recipient,part) DO UPDATE SET status='sending',error=NULL,updated_at=now()",comparison_id,call['ordinal'],evaluation.VERSION,actor.telegram_user_id,part)
                    try:
                        title='Пример оформления из сохранённых разборов' if saved_preview else 'Объединённый разбор'
                        prefix=f'<b>{title} · {call["ordinal"]}/{len(calls)}'+(f' · часть {part+1}/{len(chunks)}' if len(chunks)>1 else '')+'</b>\n\n'
                        markup=render.keyboard(comparison_id,call['ordinal']) if (row['status']=='complete' or saved_preview) and part==len(chunks)-1 else None
                        msg=await bot.send_message(actor.telegram_user_id,prefix+chunk,parse_mode='HTML',reply_markup=markup)
                    except Exception as exc:
                        await conn.execute("UPDATE combined_report_delivery SET status='failed',error=$6 WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND recipient=$4 AND part=$5",comparison_id,call['ordinal'],evaluation.VERSION,actor.telegram_user_id,part,type(exc).__name__)
                        raise RuntimeError('Combined report delivery failed') from None
                    await conn.execute("UPDATE combined_report_delivery SET status='sent',message_id=$6,updated_at=now() WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND recipient=$4 AND part=$5",comparison_id,call['ordinal'],evaluation.VERSION,actor.telegram_user_id,part,msg.message_id)
        finally:
            await conn.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))',key)
