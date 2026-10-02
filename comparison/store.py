from __future__ import annotations
import json
import secrets
from zoneinfo import ZoneInfo
from tools import Forbidden
from .registry import LEGACY, COURSE, sha, verify
from .transport import settings


def authorized(actor):
    if actor is None or actor.role not in {'head', 'owner'}:
        raise Forbidden('Сравнение доступно руководителю и владельцу.')


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


ELIGIBLE = """
 SELECT c.id,c.extension,c.transcript,c.call_started_at,c.duration_seconds,c.client_number,e.full_name
 FROM calls c JOIN astra_analysis a ON a.call_id=c.id
 JOIN employees e ON e.client_id=c.client_id AND e.extension=c.extension AND e.role='manager'
 WHERE c.client_id=$1 AND c.status='analyzed' AND a.status='analyzed'
 AND btrim(coalesce(c.transcript,''))<>'' AND c.call_started_at IS NOT NULL
 AND a.analysis IS NOT NULL AND a.level IS NOT NULL
 AND coalesce(a.analysis->>'call_cut_short','false')='false'
"""


async def select_calls(conn, client_id):
    latest = await conn.fetch('WITH eligible AS (' + ELIGIBLE + ") SELECT * FROM (SELECT DISTINCT ON(extension) * FROM eligible ORDER BY extension,call_started_at DESC,id DESC) managers ORDER BY call_started_at DESC,id DESC LIMIT 2", client_id)
    if len(latest) == 1:
        latest = await conn.fetch(ELIGIBLE + ' ORDER BY c.call_started_at DESC,c.id DESC LIMIT 2', client_id)
    return latest


async def create(pool, actor, *, new=False):
    authorized(actor)
    packages = verify()
    params = settings()
    # Pin expanded course schema, approval state, and scoring settings at creation.
    from .registry import modules
    course = modules(COURSE)
    packages[COURSE] = {**packages[COURSE], 'score_prompt': course.score_prompt(),
                       'catalog': course.approved_catalog(), 'config': course.settings()}
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', f'comparison-create:{actor.client_id}:{actor.telegram_user_id}')
        latest = await conn.fetchrow('SELECT * FROM analysis_comparisons WHERE client_id=$1 AND initiator=$2 ORDER BY id DESC LIMIT 1', actor.client_id, actor.telegram_user_id)
        if latest and (not new or latest['status'] in {'queued','running'}):
            return latest['id'], False
        calls = await select_calls(conn, actor.client_id)
        if not calls:
            raise ValueError('Нет подходящих уже разобранных звонков с транскрипцией.')
        tz = await conn.fetchval('SELECT timezone FROM clients WHERE id=$1', actor.client_id)
        run_id = await conn.fetchval('INSERT INTO analysis_comparisons(client_id,initiator,packages,settings) VALUES($1,$2,$3::jsonb,$4::jsonb) RETURNING id', actor.client_id, actor.telegram_user_id, json.dumps(packages), json.dumps(params))
        for ordinal, call in enumerate(calls, 1):
            versions = [LEGACY, COURSE]
            if secrets.randbelow(2):
                versions.reverse()
            digits = ''.join(c for c in (call['client_number'] or '') if c.isdigit())
            metadata = {'call_id': call['id'], 'manager': call['full_name'] or f"доб. {call['extension']}",
                        'extension': call['extension'], 'call_started_at': call['call_started_at'].astimezone(ZoneInfo(tz)).isoformat(),
                        'duration_seconds': call['duration_seconds'], 'timezone': tz,
                        'phone': ('+' + '*'*max(len(digits)-4, 0) + digits[-4:]) if len(digits)>=4 else 'неизвестен'}
            await conn.execute('INSERT INTO analysis_comparison_calls VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,$8)', run_id, ordinal, call['id'], call['transcript'], sha(call['transcript']), json.dumps(metadata), *versions)
            for version in (LEGACY,COURSE):
                for stage in ('score','short','review'):
                    await conn.execute('INSERT INTO analysis_comparison_stages(comparison_id,ordinal,version,stage) VALUES($1,$2,$3,$4)', run_id, ordinal, version, stage)
        await conn.execute("INSERT INTO tasks(type,client_id,input,dedup_key) VALUES('compare_analysis',$1,$2::jsonb,$3) ON CONFLICT DO NOTHING", actor.client_id, json.dumps({'comparison_id': run_id, 'max_calls': 2}), f'comparison:{run_id}')
        return run_id, True


async def get(pool, actor, run_id, *, initiator=False):
    authorized(actor)
    run = await pool.fetchrow('SELECT * FROM analysis_comparisons WHERE id=$1 AND client_id=$2', run_id, actor.client_id)
    if not run or (initiator and run['initiator'] != actor.telegram_user_id):
        raise Forbidden('Сравнение недоступно.')
    return run


async def vote(pool, actor, run_id, ordinal, choice):
    if choice not in {'a','b','equal','neither'}:
        raise ValueError('Неизвестная оценка.')
    async with pool.acquire() as conn, conn.transaction():
        run = await get(conn, actor, run_id, initiator=True)
        await conn.fetchval('SELECT id FROM analysis_comparisons WHERE id=$1 FOR UPDATE',run_id)
        # Re-read reveal after row lock, so a concurrent reveal freezes the vote.
        run = await get(conn, actor, run_id, initiator=True)
        if run['status'] not in {'ready','partial'} or run['revealed_at']:
            raise ValueError('Оценка недоступна: разбор ещё готовится или версии уже раскрыты.')
        if not await conn.fetchval('SELECT EXISTS(SELECT 1 FROM analysis_comparison_calls WHERE comparison_id=$1 AND ordinal=$2)', run_id, ordinal):
            raise ValueError('Звонок не найден.')
        await conn.execute('INSERT INTO analysis_comparison_votes(comparison_id,ordinal,voter,choice) VALUES($1,$2,$3,$4) ON CONFLICT(comparison_id,ordinal) DO UPDATE SET choice=excluded.choice,updated_at=now()', run_id, ordinal, actor.telegram_user_id, choice)
        # Only one pending comment per initiator, durably routed ahead of the ROP LLM.
        await conn.execute('UPDATE analysis_comparisons SET pending_comment_ordinal=NULL WHERE client_id=$1 AND initiator=$2',actor.client_id,actor.telegram_user_id)
        await conn.execute('UPDATE analysis_comparisons SET pending_comment_ordinal=$2 WHERE id=$1', run_id, ordinal)


async def comment(pool, actor, text):
    authorized(actor)
    if text.startswith('/'):
        return False
    async with pool.acquire() as conn, conn.transaction():
        run = await conn.fetchrow('SELECT * FROM analysis_comparisons WHERE client_id=$1 AND initiator=$2 AND pending_comment_ordinal IS NOT NULL AND revealed_at IS NULL ORDER BY id DESC LIMIT 1 FOR UPDATE', actor.client_id,actor.telegram_user_id)
        if not run:
            return False
        await conn.execute('UPDATE analysis_comparison_votes SET comment=$3,updated_at=now() WHERE comparison_id=$1 AND ordinal=$2 AND voter=$4',run['id'],run['pending_comment_ordinal'],text[:4000],actor.telegram_user_id)
        await conn.execute('UPDATE analysis_comparisons SET pending_comment_ordinal=NULL WHERE id=$1',run['id'])
        return run['id']


async def reveal(pool, actor, run_id):
    async with pool.acquire() as conn, conn.transaction():
        run = await get(conn,actor,run_id,initiator=True)
        await conn.fetchval('SELECT id FROM analysis_comparisons WHERE id=$1 FOR UPDATE', run_id)
        counts = await conn.fetchrow('SELECT (SELECT count(*) FROM analysis_comparison_calls WHERE comparison_id=$1) total,(SELECT count(*) FROM analysis_comparison_votes WHERE comparison_id=$1) voted',run_id)
        if not counts['total'] or counts['total'] != counts['voted']:
            raise ValueError('Сначала оцените каждый звонок.')
        await conn.execute('UPDATE analysis_comparisons SET revealed_at=coalesce(revealed_at,now()),pending_comment_ordinal=NULL WHERE id=$1',run_id)
