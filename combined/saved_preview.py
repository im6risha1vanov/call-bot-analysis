"""Explicit offline formatting preview; never pretend a failed model request succeeded."""
from __future__ import annotations
import copy,json,re
from comparison.engine import parse
from comparison.registry import COURSE,LEGACY,modules
from comparison.store import decode,get as get_comparison
from . import evaluation,jobs


def assemble(course_raw,legacy_review,transcript,*,config,catalog):
    base=modules(COURSE).evaluate(copy.deepcopy(course_raw),transcript,config=config,commercial_catalog=catalog)
    valid=modules(COURSE).evidence_valid
    normalize=lambda s:' '.join(s.split())
    def matches(quote,row):
        if not valid(quote,transcript):return False
        a=normalize(quote);b=normalize(row['evidence'])
        return bool(b and min(len(a),len(b))>=12 and (a in b or b in a))
    manager=legacy_review.get('for_manager') or {}
    passed=[r for r in base['rows'] if r['status']=='passed']
    failed=sorted([r for r in base['rows'] if r['status']=='failed'],key=lambda r:0 if r['key']=='next_step' else 1)
    strengths=[]
    for row in passed[:2]:
        legacy=next((v for v in manager.get('did_well',[]) if isinstance(v,dict) and matches(v.get('quote',''),row)),None)
        strengths.append({'criterion':row['key'],'quote':legacy['quote'] if legacy else row['evidence'],
                          'what':legacy.get('what') if legacy else row['title'],
                          'why':legacy.get('why') if legacy else row['reason']})
    moments=[]
    for row in failed:
        legacy=next((v for v in manager.get('moments',[]) if isinstance(v,dict) and matches(v.get('quote',''),row)),None)
        if not legacy:continue  # Do not transfer legacy-only criticisms to the new applicability rules.
        quotes=re.findall('«([^»]+)»',legacy.get('reaction') or '')
        reaction=next((q for q in quotes if valid(q,transcript)),None)
        moments.append({'criterion':row['key'],'quote':legacy['quote'],'reaction_quote':reaction,
                        'what':row['title'],'why':legacy.get('why') or row['reason'],
                        'say_instead':legacy.get('say_instead') or ''})
        if len(moments)==2:break
    if not moments and failed:
        row=failed[0]
        moments=[{'criterion':row['key'],'quote':row['evidence'],'reaction_quote':None,
                  'what':row['title'],'why':row['reason'],'say_instead':base['coaching']['say_instead']}]
    miss=failed[0] if failed else None
    raw=copy.deepcopy(course_raw)
    raw['readable_report']={
        'summary':base['outcome']['description'], 'strengths':strengths,'moments':moments,
        'main_miss':{'criterion':miss['key'],'quote':miss['evidence'],'explanation':miss['reason']} if miss else None,
        'exercise':manager.get('practice'),'next_focus':manager.get('next_call_focus'),
        'leader':{'cause':'insufficient_data','quotes':[],'reason':'Причина по одному разговору не установлена.',
                  'action':(legacy_review.get('for_head') or {}).get('coaching_action')},
        'limits':['Пример оформления составлен из сохранённых разборов. Новый запрос к модели не вернул ответ; это не новая оценка звонка.']}
    result=evaluation.evaluate(raw,transcript,config=config,catalog=catalog)
    result['data_origin']='saved_analyses_preview'
    return result


async def prepare(pool,actor,comparison_id,ordinal):
    await get_comparison(pool,actor,comparison_id,initiator=True)
    row=await jobs.get(pool,actor,comparison_id,ordinal)
    if row['status'] not in {'failed','uncertain'} or row['response_text'] is not None:
        raise ValueError('Offline preview requires an unsuccessful request with no receipt')
    if row['result']:
        return False
    course=await pool.fetchval("SELECT response_text FROM analysis_comparison_stages WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND stage='score' AND status='complete'",comparison_id,ordinal,COURSE)
    legacy=await pool.fetchval("SELECT result FROM analysis_comparison_stages WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND stage='review' AND status='complete'",comparison_id,ordinal,LEGACY)
    if not course or not legacy:raise ValueError('Both saved sources are required')
    result=assemble(parse(course),decode(legacy),row['transcript'],config=decode(row['config']),catalog=decode(row['catalog']))
    # Keep the failed/uncertain status and unknown spend; this is not a provider receipt.
    await pool.execute("UPDATE combined_reports SET result=$4::jsonb WHERE comparison_id=$1 AND ordinal=$2 AND version=$3 AND result IS NULL AND response_text IS NULL AND status IN ('failed','uncertain')",comparison_id,ordinal,evaluation.VERSION,json.dumps(result,ensure_ascii=False))
    return True
