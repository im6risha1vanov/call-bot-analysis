#!/usr/bin/env python3
"""Installed outside the checkout so it survives a complete code rollback."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

PROJECT = Path('/opt/callbot-astra')
STATE = Path('/opt/callbot-astra-releases')
BASELINE = '52e4d575c03788d09194346362b1e06ef4476feb'
UNITS = ['callbot-astra.service', 'callbot-astra-worker.service',
         'callbot-astra-queue-runner.service', 'callbot-astra-trainer.service']


def command(args, *, cwd=PROJECT):
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Do not expose remote URLs, database credentials or arbitrary journal text.
        raise RuntimeError(f'Command failed: {args[0]} (exit {result.returncode})')
    return result.stdout.strip()


def guard():
    if socket.gethostname().split('.')[0] != '9015421-nt422325' or PROJECT.resolve() != Path('/opt/callbot-astra'):
        raise RuntimeError('Wrong server or checkout')
    if os.geteuid() != 0:
        raise RuntimeError('Run with sudo: switching all four services requires root')


def commit(ref):
    if not re.fullmatch('[0-9a-f]{40}', ref):
        raise RuntimeError('Only pinned full commit IDs are accepted')
    resolved = command(['git', 'rev-parse', '--verify', ref+'^{commit}'])
    if resolved != ref:
        raise RuntimeError('Commit mismatch')
    return resolved


def manifest(ref):
    files = subprocess.check_output(['git','ls-tree','-r','--name-only','-z',ref],cwd=PROJECT).split(b'\0')
    return {name.decode():hashlib.sha256(subprocess.check_output(['git','show',ref+':'+name.decode()],cwd=PROJECT)).hexdigest()
            for name in files if name}


def verify_files(ref):
    expected = manifest(ref)
    if ref == BASELINE and (STATE/'original-manifest.json').exists():
        if expected != json.loads((STATE/'original-manifest.json').read_text()):
            raise RuntimeError('Original Git files no longer match saved baseline manifest')
    if command(['git','rev-parse','HEAD']) != ref:
        raise RuntimeError('HEAD mismatch')
    for name,digest in expected.items():
        path=PROJECT/name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise RuntimeError('Tracked file checksum mismatch')
    if command(['git','status','--porcelain']):
        raise RuntimeError('Checkout is not clean')
    return len(expected)


async def database(action, *, release=None):
    import asyncpg
    from dotenv import dotenv_values
    dsn = dotenv_values(PROJECT/'.env').get('DATABASE_URL')
    if not dsn:
        raise RuntimeError('Database configuration is missing')
    conn = await asyncpg.connect(dsn)
    try:
        if action=='migrate':
            migration=subprocess.check_output(['git','show',release+':migrations/001_course_methodology.sql'],cwd=PROJECT).decode()
            await conn.execute(migration)
            return
        processing=await conn.fetchval("SELECT count(*) FROM tasks WHERE status='processing'")
        course_active=0
        if await conn.fetchval("SELECT to_regclass('methodology_training_context') IS NOT NULL"):
            course_active=await conn.fetchval("SELECT count(*) FROM training_sessions t JOIN methodology_training_context mc ON mc.session_id=t.id WHERE t.status='active'")
        return {'processing_tasks':processing,'active_course_training':course_active}
    finally:
        await conn.close()


def healthy():
    return all(command(['systemctl','is-active',unit])=='active' for unit in UNITS)


def wait_healthy():
    for _ in range(10):
        try:
            if healthy():
                time.sleep(2)
                if healthy():
                    return
        except RuntimeError:
            pass
        time.sleep(1)
    raise RuntimeError('Not all Astra services became active')


def switch(target, *, rollback=False):
    commit(target)
    if command(['git','status','--porcelain']):
        raise RuntimeError('Uncommitted changes: save them before switching releases')
    current=command(['git','rev-parse','HEAD'])
    busy=asyncio.run(database('status'))
    if busy['processing_tasks'] or busy['active_course_training']:
        raise RuntimeError('Processing is in progress. Wait for tasks and finish course training with /stop before switching')
    if not rollback:
        asyncio.run(database('migrate',release=target))
    command(['systemctl','stop',*UNITS])
    try:
        # A task might have been claimed between the initial read and service shutdown.
        busy=asyncio.run(database('status'))
        if busy['processing_tasks'] or busy['active_course_training']:
            raise RuntimeError('Work started during shutdown; restoring running services without changing code')
        command(['git','checkout','--detach',target])
        files=verify_files(target)
        command(['systemctl','start',*UNITS])
        wait_healthy()
    except BaseException:
        command(['git','checkout','--detach',current])
        command(['systemctl','start',*UNITS])
        wait_healthy()
        raise
    print(json.dumps({'commit':target,'verified_tracked_files':files,'services':'4 active',
                      'database':'preserved; no down migration'},ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['init','status','deploy','rollback'])
    parser.add_argument('--release')
    args=parser.parse_args()
    guard()
    config_path=STATE/'release.json'
    if args.action=='init':
        if config_path.exists():
            raise RuntimeError('Release configuration already exists')
        release=commit(args.release or '')
        commit(BASELINE)
        STATE.mkdir(exist_ok=True,mode=0o755)
        (STATE/'original-manifest.json').write_text(json.dumps(manifest(BASELINE),indent=2)+'\n')
        config_path.write_text(json.dumps({'baseline':BASELINE,'release':release},indent=2)+'\n')
        print('Pinned baseline and release; original file checksums saved')
        return
    config=json.loads(config_path.read_text())
    if config['baseline']!=BASELINE:
        raise RuntimeError('Unexpected rollback baseline')
    if args.action=='status':
        print(json.dumps({'commit':command(['git','rev-parse','HEAD']), 'services':[{'unit':u,'active':command(['systemctl','is-active',u])} for u in UNITS],
                          **asyncio.run(database('status'))}))
    else:
        switch(config['baseline'] if args.action=='rollback' else config['release'],rollback=args.action=='rollback')


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print(f'Release operation stopped: {str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__}',file=sys.stderr)
        sys.exit(1)
