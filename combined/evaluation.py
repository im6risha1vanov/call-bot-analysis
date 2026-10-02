"""Course applicability with legacy narrative depth, without inventing numeric weights."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from comparison.registry import COURSE, modules, verify as verify_packages, sha

ROOT=Path(__file__).parent
VERSION='combined_2026_v1'


def verify():
    manifest=json.loads((ROOT/'manifest.json').read_text())
    if manifest['version']!=VERSION or manifest['course_sha256']!=verify_packages()[COURSE]['sha256']:
        raise ValueError('Combined base package mismatch')
    for name,digest in manifest['files'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest:
            raise ValueError('Combined package hash mismatch')
    if sha(json.dumps({'files':manifest['files'],'course_sha256':manifest['course_sha256']},sort_keys=True))!=manifest['sha256']:
        raise ValueError('Combined manifest mismatch')
    return manifest


def prompt():
    verify()
    return modules(COURSE).score_prompt()+'\n\n'+(ROOT/'prompt.md').read_text()


def text(value,limit=300):
    if value is None:
        return ''
    if not isinstance(value,str):
        raise ValueError('Readable report requires text fields')
    # Telegram paragraphs are rendered by code; models cannot introduce HTML sections.
    value=' '.join(value.split())
    return value if len(value)<=limit else value[:limit-1].rstrip()+'…'


def evaluate(raw,transcript,*,config,catalog):
    course=modules(COURSE)
    result=course.evaluate(raw,transcript,config=config,commercial_catalog=catalog)
    data=raw.get('readable_report')
    if not isinstance(data,dict):
        raise ValueError('Readable report missing')
    rows={r['key']:r for r in result['rows']}
    warnings=[]
    def items(key,limit):
        values=data.get(key,[])
        if not isinstance(values,list) or len(values)>limit or any(not isinstance(v,dict) for v in values):
            raise ValueError('Invalid readable report list')
        return values
    def supported(item,status):
        criterion=item.get('criterion')
        if not isinstance(criterion,str) or criterion not in rows or rows[criterion]['status']!=status or not course.evidence_valid(item.get('quote'),transcript):
            warnings.append('Часть наблюдений модели не прошла проверку цитат или применимости и исключена.')
            return False
        return True
    strengths=[]
    for item in items('strengths',2):
        if supported(item,'passed'):
            strengths.append({'criterion':item['criterion'],'quote':item['quote'],
                              'what':text(item.get('what'),160),'why':text(item.get('why'),250)})
    moments=[]
    for item in items('moments',2):
        if supported(item,'failed'):
            reaction=item.get('reaction_quote')
            if reaction is not None and not isinstance(reaction,str):
                raise ValueError('Invalid reaction quote')
            if reaction and not course.evidence_valid(reaction,transcript):
                warnings.append('Неподтверждённая реакция клиента исключена.');reaction=None
            moments.append({'criterion':item['criterion'],'quote':item['quote'],'reaction_quote':reaction,
                            'what':text(item.get('what'),160),'why':text(item.get('why'),250),
                            'say_instead':text(item.get('say_instead'),280)})
    miss=data.get('main_miss')
    if miss is not None and not isinstance(miss,dict):
        raise ValueError('Invalid main miss')
    if miss and supported(miss,'failed'):
        miss={'criterion':miss['criterion'],'quote':miss['quote'],'explanation':text(miss.get('explanation'),350)}
    else:
        miss=None
    leader=data.get('leader') or {}
    if not isinstance(leader,dict) or leader.get('cause') not in {'manager','script','base','insufficient_data'}:
        raise ValueError('Invalid leader diagnosis')
    quotes=leader.get('quotes',[])
    if not isinstance(quotes,list) or len(quotes)>2 or any(not isinstance(q,str) for q in quotes):
        raise ValueError('Invalid leader quotes')
    quotes=[q for q in quotes if course.evidence_valid(q,transcript)]
    cause=leader['cause'] if quotes else 'insufficient_data'
    reason=text(leader.get('reason'),280) if cause!='insufficient_data' else 'Материала недостаточно, чтобы уверенно определить причину.'
    limits=data.get('limits',[])
    if not isinstance(limits,list) or len(limits)>2 or any(not isinstance(v,str) for v in limits):
        raise ValueError('Invalid limitations')
    result.update(methodology_version=VERSION,readable_report={
        'summary':text(data.get('summary'),180),'strengths':strengths,'moments':moments,'main_miss':miss,
        'exercise':text(data.get('exercise'),220),'next_focus':text(data.get('next_focus'),220),
        'leader':{'cause':cause,'quotes':quotes,'reason':reason,'action':text(leader.get('action'),280)},
        'limits':[text(v,260) for v in limits], 'validation_warnings':list(dict.fromkeys(warnings))})
    return result
