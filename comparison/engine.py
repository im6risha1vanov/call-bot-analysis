"""Fresh independent analyses, with a write-ahead receipt for every paid stage."""
from __future__ import annotations
import json
import types
from tools import resolve_actor
from . import transport
from .registry import LEGACY, COURSE, modules, prompt, verify, sha
from .store import decode, authorized


def parse(text):
    raw = json.loads(text.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip(),
                     parse_constant=lambda s: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    if not isinstance(raw,dict):
        raise ValueError('JSON object required')
    return raw


def validate_legacy(raw, stage):
    if stage == 'score':
        if type(raw.get('call_cut_short',False)) is not bool or not isinstance(raw.get('criteria'),dict):
            raise ValueError('Invalid score JSON')
        expected={k for k,_,_ in modules(LEGACY).CRITERIA}
        if set(raw['criteria'])!=expected:
            raise ValueError('All twelve original criteria required')
        for item in raw['criteria'].values():
            if not isinstance(item,dict) or type(item.get('passed')) is not bool or type(item.get('applicable',True)) is not bool or not isinstance(item.get('evidence',''),str):
                raise ValueError('Invalid criterion')
        for key in ('meeting_booked','proposal_sent','decision_maker_contact'):
            if type(raw.get(key,False)) is not bool:
                raise ValueError('Invalid outcome flag')
        if not isinstance(raw.get('signals',{}),dict):
            raise ValueError('Invalid signals')
    elif stage == 'short':
        if not isinstance(raw.get('result'),str) or any(not isinstance(raw.get(k),list) or any(not isinstance(i,dict) for i in raw[k]) for k in ('good','weak')):
            raise ValueError('Invalid short report')
    elif stage == 'review':
        if not isinstance(raw.get('for_head'),dict) or not isinstance(raw.get('for_manager'),dict):
            raise ValueError('Invalid detailed report')
        for block,key in (('for_head','skill_gaps'),('for_manager','did_well'),('for_manager','moments')):
            if not isinstance(raw[block].get(key,[]),list) or any(not isinstance(i,dict) for i in raw[block].get(key,[])):
                raise ValueError('Invalid detailed list')
    return raw


def legacy_level(mod, raw, rows, params):
    config = params['legacy_verdict']
    namespace = dict(mod.compute_level.__globals__)
    namespace.update(LEVEL_CRITICAL_CRITERIA=set(config['keys']), VERDICT_MIN_FAILURES=config['min_failures'], VERDICT_MIN_FAILURES_WITH_SIGNAL=config['min_with_signal'])
    return types.FunctionType(mod.compute_level.__code__,namespace)(raw,rows)


async def run(pool, run_id):
    # One worker per comparison, even across duplicate tasks and processes.
    async with pool.acquire() as conn:
        key = f'comparison-run:{run_id}'
        if not await conn.fetchval('SELECT pg_try_advisory_lock(hashtextextended($1,0))',key):
            return {'comparison_id':run_id,'busy':True}
        try:
            experiment = await conn.fetchrow('SELECT * FROM analysis_comparisons WHERE id=$1',run_id)
            if not experiment:
                raise ValueError('Missing comparison')
            actor = await resolve_actor(conn,experiment['initiator'])
            authorized(actor)
            if actor.client_id != experiment['client_id']:
                raise PermissionError('Initiator binding changed')
            manifest = verify(); pinned = decode(experiment['packages']); params=decode(experiment['settings'])
            for version in (LEGACY,COURSE):
                if manifest[version]['sha256'] != pinned[version]['sha256']:
                    raise ValueError('Pinned package unavailable')
            calls = await conn.fetch('SELECT * FROM analysis_comparison_calls WHERE comparison_id=$1 ORDER BY ordinal',run_id)
            if not 1 <= len(calls) <= 2:
                raise ValueError('Comparison requires at most two calls')
            await conn.execute("UPDATE analysis_comparisons SET status='running',updated_at=now() WHERE id=$1",run_id)
            for call in calls:
                if sha(call['transcript']) != call['transcript_sha256']:
                    raise ValueError('Transcript snapshot hash mismatch')
                for version in (call['a_version'],call['b_version']):
                    mod=modules(version)
                    for stage in ('score','short','review'):
                        row=await conn.fetchrow('SELECT * FROM analysis_comparison_stages WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND stage=$4',run_id,call['ordinal'],version,stage)
                        if row['status']=='complete':
                            continue
                        if row['status'] in {'failed','uncertain','processing'} and row['response_text'] is None:
                            if row['status']=='processing':
                                await conn.execute("UPDATE analysis_comparison_stages SET status='uncertain',error='resume after interrupted request' WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND stage=$4",run_id,call['ordinal'],version,stage)
                            break  # Never repeat an uncertain paid request.
                        previous=await conn.fetchrow("SELECT result FROM analysis_comparison_stages WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND stage='score' AND status='complete'",run_id,call['ordinal'],version)
                        if stage != 'score' and not previous:
                            break
                        try:
                            if version==LEGACY and stage=='short' and decode(previous['result'])['level'] is None:
                                result={'result':'нет данных — звонок обрывочный или короче 30 секунд','good':[{'time':None,'text':'нечем'}],'weak':[]}
                            elif version==COURSE and stage!='score':
                                result={'text':mod.render(decode(previous['result']),detailed=stage=='review')}
                            else:
                                text=row['response_text']
                                if text is None:
                                    user='КОНТЕКСТ (не заменяет цитаты):\n'+json.dumps(decode(call['metadata']),ensure_ascii=False)+'\n\nТРАНСКРИПТ:\n'+call['transcript']
                                    if stage!='score':
                                        score=decode(previous['result']); brief=mod.build_brief(score['raw'],score['rows'])
                                        user+='\n\n---\n\nРЕЗУЛЬТАТЫ ПРОВЕРКИ:\n'+brief
                                        if stage=='short':
                                            user='УРОВЕНЬ: '+str(score['level'])+'\n\n'+user
                                    instructions=pinned[COURSE]['score_prompt'] if version==COURSE else prompt(version,stage)
                                    await conn.execute("UPDATE analysis_comparison_stages SET status='processing',started_at=now() WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND stage=$4",run_id,call['ordinal'],version,stage)
                                    text,cost,usage=await transport.request(instructions,user,params)
                                    # Receipt first: validation/formatting failure never repeats billing.
                                    await conn.execute("UPDATE analysis_comparison_stages SET status='received',response_text=$5,cost_units=$6,usage=$7::jsonb WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND stage=$4",run_id,call['ordinal'],version,stage,text,cost,json.dumps(usage))
                                raw=parse(text)
                                if version==COURSE:
                                    result=mod.evaluate(raw,call['transcript'],config=pinned[COURSE]['config'],commercial_catalog=pinned[COURSE]['catalog'])
                                else:
                                    validate_legacy(raw,stage)
                                    if stage=='score':
                                        number,rows=mod.compute_score(raw)
                                        result={'raw':raw,'score':number,'rows':rows,'level':legacy_level(mod,raw,rows,params)}
                                    else:
                                        result=raw
                            await conn.execute("UPDATE analysis_comparison_stages SET status='complete',result=$5::jsonb,error=NULL,completed_at=now() WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND stage=$4",run_id,call['ordinal'],version,stage,json.dumps(result,ensure_ascii=False))
                        except transport.RequestFailure as exc:
                            state='failed' if exc.status in {400,401,402,403,404,429} else 'uncertain'
                            await conn.execute('UPDATE analysis_comparison_stages SET status=$5,error=$6,completed_at=now() WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND stage=$4',run_id,call['ordinal'],version,stage,state,str(exc))
                            break
                        except (ValueError,TypeError,KeyError,AttributeError) as exc:
                            await conn.execute("UPDATE analysis_comparison_stages SET status='failed',error=$5,completed_at=now() WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND stage=$4",run_id,call['ordinal'],version,stage,type(exc).__name__)
                            break
            total=await conn.fetchrow("SELECT count(*) total,count(*) FILTER(WHERE status='complete') complete FROM analysis_comparison_stages WHERE comparison_id=$1",run_id)
            status='ready' if total['total']==total['complete'] else 'partial'
            await conn.execute('UPDATE analysis_comparisons SET status=$2,updated_at=now() WHERE id=$1',run_id,status)
            return {'comparison_id':run_id,'status':status}
        finally:
            await conn.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))',key)
