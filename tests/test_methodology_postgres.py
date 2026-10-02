import asyncio
import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault('CVC_API_KEY', 'synthetic-test-key')
os.environ.setdefault('BOT_TOKEN', '123456:synthetic_test_token')
os.environ.setdefault('TRAIN_BOT_TOKEN', '123457:synthetic_test_token')
os.environ.setdefault('ANTHROPIC_API_KEY', 'synthetic-test-key')
os.environ.setdefault('ENCRYPTION_KEY', base64.urlsafe_b64encode(b'0'*32).decode())

import asyncpg
from methodology import jobs, training, transport
from methodology.runtime import VERSION
from methodology.tools import get_course_evaluation, get_course_stats
from tools import Actor, Forbidden
from tests.test_methodology import TRANSCRIPT, response

DSN = os.environ.get('METHODOLOGY_TEST_DATABASE_URL')


@unittest.skipUnless(DSN, 'Requires a new isolated test database')
class PostgreSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=5)
        name = await self.pool.fetchval('SELECT current_database()')
        self.assertTrue(name.startswith('callbot_methodology_test_'), 'Refusing any non-test database')
        exists = await self.pool.fetchval("SELECT to_regclass('clients') IS NOT NULL")
        root = Path(__file__).resolve().parents[1]
        if not exists:
            await self.pool.execute((root/'tests/bootstrap.sql').read_text())
        await self.pool.execute((root/'migrations/001_course_methodology.sql').read_text())
        await self.pool.execute('TRUNCATE clients CASCADE')
        await self.pool.execute("INSERT INTO clients VALUES(1,'Europe/Moscow',true),(2,'UTC',true)")
        await self.pool.execute("INSERT INTO employees(client_id,extension,telegram_user_id,role) VALUES(1,'101',123,'manager'),(1,'102',124,'manager'),(2,'201',223,'manager')")
        self.actor = Actor(123,1,'manager','101')
        self.call_id = await self.pool.fetchval("INSERT INTO calls(client_id,extension,transcript) VALUES(1,'101',$1) RETURNING id", TRANSCRIPT)
        await self.pool.execute("INSERT INTO astra_analysis VALUES($1,87,'✅')", self.call_id)
        from handlers.evaluate_course import evaluate_course
        self.handler = evaluate_course
        self.task = {'id':1, 'client_id':1,'input':json.dumps({'source':'call','source_id':self.call_id,'version':VERSION,'sha256':jobs.fingerprint(TRANSCRIPT)})}

    async def asyncTearDown(self):
        await self.pool.close()

    async def test_receipt_cache_and_budget_once_legacy_untouched(self):
        fake = AsyncMock(return_value=(json.dumps(response()), 1234.5))
        with patch.object(transport,'request',fake):
            a = await self.handler(self.pool,self.task)
            b = await self.handler(self.pool,self.task)
        self.assertEqual(fake.await_count,1)
        self.assertEqual(a['evaluation_id'],b['evaluation_id'])
        self.assertTrue(b['cached'])
        self.assertEqual(float(await self.pool.fetchval('SELECT sum(spent_units) FROM methodology_daily_spend')),1234.5)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM astra_daily_spend'),0)
        self.assertEqual(await self.pool.fetchval('SELECT score FROM astra_analysis WHERE call_id=$1',self.call_id),87)

    async def test_invalid_json_charged_once_not_repaid(self):
        fake=AsyncMock(return_value=('{broken',22))
        with patch.object(transport,'request',fake):
            self.assertEqual((await self.handler(self.pool,self.task))['status'],'failed')
            await self.handler(self.pool,self.task)
        self.assertEqual(fake.await_count,1)
        self.assertEqual(float(await self.pool.fetchval('SELECT sum(spent_units) FROM methodology_daily_spend')),22)

    async def test_network_unknown_and_http_rejection_no_retry(self):
        for code, state in [(None,'uncertain'),(402,'failed')]:
            await self.pool.execute('DELETE FROM methodology_evaluations')
            fake=AsyncMock(side_effect=transport.ProviderFailure(code))
            with patch.object(transport,'request',fake):
                self.assertEqual((await self.handler(self.pool,self.task))['status'],state)
                await self.handler(self.pool,self.task)
            self.assertEqual(fake.await_count,1)

    async def test_restart_pending_request_never_repaid(self):
        from handlers.evaluate_course import restore_course
        await self.pool.execute("INSERT INTO methodology_evaluations(client_id,call_id,version,transcript_sha256,status) VALUES(1,$1,$2,$3,'processing')", self.call_id,VERSION,jobs.fingerprint(TRANSCRIPT))
        await restore_course(self.pool)
        fake=AsyncMock()
        with patch.object(transport,'request',fake):
            result=await self.handler(self.pool,self.task)
        self.assertEqual(result['status'],'uncertain')
        fake.assert_not_awaited()

    async def test_restart_after_receipt_validates_without_charge(self):
        client=await self.pool.fetchrow('SELECT * FROM clients WHERE id=1')
        eid=await self.pool.fetchval("INSERT INTO methodology_evaluations(client_id,call_id,version,transcript_sha256,status) VALUES(1,$1,$2,$3,'processing') RETURNING id", self.call_id,VERSION,jobs.fingerprint(TRANSCRIPT))
        await jobs.store_receipt(self.pool,eid,client,json.dumps(response()),19)
        await jobs.store_receipt(self.pool,eid,client,json.dumps(response()),19)
        fake=AsyncMock()
        with patch.object(transport,'request',fake):
            await self.handler(self.pool,self.task)
        fake.assert_not_awaited()
        self.assertEqual(float(await self.pool.fetchval('SELECT sum(spent_units) FROM methodology_daily_spend')),19)

    async def test_disabled_processing_defers_without_model(self):
        from queue_runner import RetryLater
        await self.pool.execute('UPDATE clients SET processing_enabled=false WHERE id=1')
        fake=AsyncMock()
        with patch.object(transport,'request',fake),self.assertRaises(RetryLater):
            await self.handler(self.pool,self.task)
        fake.assert_not_awaited()

    async def test_shadow_disabled_is_no_op(self):
        from queue_runner import RetryLater
        fake=AsyncMock()
        with patch('handlers.evaluate_course.shadow_enabled',return_value=False),patch.object(transport,'request',fake):
            with self.assertRaises(RetryLater):
                await self.handler(self.pool,self.task)
        fake.assert_not_awaited()
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM methodology_evaluations'),0)

    async def test_dedup_and_tenant_enforcement(self):
        await jobs.enqueue(self.pool,1,'call',self.call_id,TRANSCRIPT)
        await jobs.enqueue(self.pool,1,'call',self.call_id,TRANSCRIPT)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM tasks'),1)
        task=dict(self.task); task['client_id']=2
        fake=AsyncMock()
        with patch.object(transport,'request',fake):
            self.assertIn('skipped',await self.handler(self.pool,task))
        fake.assert_not_awaited()
        with self.assertRaises(Forbidden):
            await get_course_evaluation(self.pool,Actor(223,2,'owner',None),call_id=self.call_id)
        with self.assertRaises(Forbidden):
            await get_course_evaluation(self.pool,Actor(124,1,'manager','102'),call_id=self.call_id)

    async def test_stats_exclude_other_tenants_and_na_from_denominator(self):
        raw=response(); raw['criteria']['relevant_presentation']['status']='not_applicable'
        with patch.object(transport,'request',AsyncMock(return_value=(json.dumps(raw),1))):
            await self.handler(self.pool,self.task)
        stats=await get_course_stats(self.pool,self.actor,{'start':'2020-01-01','end':'2030-01-01'},manager_extension='102')
        self.assertEqual(stats['criteria']['relevant_presentation']['measured'],0)
        self.assertEqual(stats['criteria']['relevant_presentation']['not_applicable'],1)
        other=await get_course_stats(self.pool,Actor(223,2,'owner',None),{'start':'2020-01-01','end':'2030-01-01'})
        self.assertEqual(other['criteria'],{})

    async def test_concurrent_start_creates_one_session(self):
        result=await asyncio.gather(training.start(self.pool,self.actor,'owner_minute',None,'dialog'),
                                    training.start(self.pool,self.actor,'owner_minute',None,'dialog'),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,training.TrainingLimit) for x in result),1)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM training_sessions WHERE status='active'"),1)
        row=await self.pool.fetchrow('SELECT * FROM training_sessions')
        self.assertEqual(json.loads(row['transcript'])[0]['text'],'Алло, кто это?')

    async def test_drill_context_persisted_and_final_task_atomic(self):
        session=await training.start(self.pool,self.actor,'expensive',None,'drill')
        for _ in range(5):
            result=await training.turn(self.pool,session,'С чем сравниваете стоимость?')
        self.assertTrue(result.ended)
        row=await self.pool.fetchrow('SELECT * FROM training_sessions WHERE id=$1',session['id'])
        self.assertEqual(row['status'],'completed')
        self.assertIsNone(row['score'])
        self.assertEqual(len(json.loads(row['drill_state'])['results']),5)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM tasks'),1)

    async def test_concurrent_messages_and_stop_do_not_repeat_paid_turn(self):
        import training_simulator as old
        session=await training.start(self.pool,self.actor,'owner_minute',None,'dialog')
        entered=asyncio.Event(); release=asyncio.Event()
        async def reply(**kwargs):
            entered.set(); await release.wait()
            return SimpleNamespace(usage=SimpleNamespace(input_tokens=100,output_tokens=30),content=[SimpleNamespace(type='text',text='Да, слушаю.')])
        fake=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=reply)))
        with patch.object(old.claude,'with_options',return_value=fake):
            first=asyncio.create_task(training.turn(self.pool,session,'Здравствуйте, я по ГЦК.'))
            await entered.wait()
            second=await training.turn(self.pool,session,'Ещё ответ')
            stop=await training.turn(self.pool,session,stop=True)
            self.assertIn('обрабатывается',second.note)
            self.assertIn('обрабатывается',stop.note)
            release.set(); await first
        self.assertEqual(fake.messages.create.await_count,1)
        row=await self.pool.fetchrow('SELECT * FROM training_sessions WHERE id=$1',session['id'])
        self.assertEqual(sum(t['role']=='manager' for t in json.loads(row['transcript'])),1)
        self.assertEqual((await training.context(self.pool,session['id']))['turn_state'],'ready')

    async def test_course_turn_provider_failure_cannot_be_retried(self):
        import training_simulator as old
        session=await training.start(self.pool,self.actor,'owner_minute',None,'dialog')
        fake=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=TimeoutError())))
        with patch.object(old.claude,'with_options',return_value=fake):
            self.assertTrue((await training.turn(self.pool,session,'Здравствуйте')).ended)
            self.assertTrue((await training.turn(self.pool,session,'Повтор')).ended)
        self.assertEqual(fake.messages.create.await_count,1)

    async def test_original_session_uses_original_handler(self):
        import training_simulator as old
        session=await self.pool.fetchrow("INSERT INTO training_sessions(client_id,extension,transcript,mode,drill_state) VALUES(1,'101','[{\"role\":\"client\",\"text\":\"Дорого\"}]','drill','{\"objections\":[\"Дорого\"],\"results\":[]}') RETURNING *")
        with patch.object(old,'_judge_answer',AsyncMock(return_value=(True,'ok',0))):
            result=await old.handle_drill_turn(self.pool,session,'Ответ')
        self.assertTrue(result.ended)
        self.assertEqual(await self.pool.fetchval('SELECT score FROM training_sessions WHERE id=$1',session['id']),100)

    async def test_feedback_failure_reuses_evaluation_and_acknowledged_parts(self):
        import handlers.evaluate_course as handler
        session=await training.start(self.pool,self.actor,'owner_minute',None,'dialog')
        turns=[{'role':'client','text':'Алло?'},{'role':'manager','text':TRANSCRIPT}]
        await self.pool.execute("UPDATE training_sessions SET transcript=$2::jsonb,status='completed' WHERE id=$1", session['id'],json.dumps(turns))
        raw=response(stage='discovery')
        for item in raw['criteria'].values():
            item['reason']='Проверяемая причина. '*100
        task={'id':2,'client_id':1,'input':json.dumps({'source':'training','source_id':session['id'],'version':VERSION,'sha256':jobs.fingerprint(training.transcript_text(turns))})}
        fake_provider=AsyncMock(return_value=(json.dumps(raw),12))
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=[None,RuntimeError('delivery failure')]),session=SimpleNamespace(close=AsyncMock()))
        with patch.object(transport,'request',fake_provider),patch.object(handler,'Bot',return_value=bot):
            with self.assertRaises(RuntimeError):
                await self.handler(self.pool,task)
            self.assertEqual(await self.pool.fetchval('SELECT feedback_parts_sent FROM methodology_evaluations'),1)
            self.assertFalse(await self.pool.fetchval('SELECT feedback_sent FROM methodology_evaluations'))
            bot.send_message.side_effect=None
            result=await self.handler(self.pool,task)
        self.assertTrue(result['cached'])
        self.assertEqual(fake_provider.await_count,1)
        self.assertTrue(await self.pool.fetchval('SELECT feedback_sent FROM methodology_evaluations'))
        self.assertTrue(all(c.kwargs.get('parse_mode','missing') is None for c in bot.send_message.call_args_list))

    async def test_two_evaluators_pay_once(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def reply(*args):
            entered.set();await release.wait()
            return json.dumps(response()),7
        fake=AsyncMock(side_effect=reply)
        with patch.object(transport,'request',fake):
            first=asyncio.create_task(self.handler(self.pool,self.task))
            await entered.wait()
            second=await self.handler(self.pool,self.task)
            self.assertTrue(second['needs_review'])
            release.set();await first
        self.assertEqual(fake.await_count,1)

    async def test_repeat_changes_circumstances_and_fourth_session_is_allowed(self):
        first=await training.start(self.pool,self.actor,'owner_minute',None,'dialog')
        # Only answered, completed sessions are eligible for a repeat.
        await self.pool.execute("UPDATE training_sessions SET transcript=$2::jsonb WHERE id=$1", first['id'],
                                json.dumps([{'role':'client','text':'Слушаю.'},{'role':'manager','text':'Здравствуйте.'}]))
        await training.turn(self.pool,first,stop=True)
        second=await training.start(self.pool,self.actor,'repeat',None,'dialog')
        scenario=json.loads((await training.context(self.pool,second['id']))['scenario'])
        self.assertEqual(scenario['facts']['available_minutes'],1)
        await training.turn(self.pool,second,stop=True)
        third=await training.start(self.pool,self.actor,'owner_minute',None,'drill')
        await training.turn(self.pool,third,stop=True)
        fourth=await training.start(self.pool,self.actor,'support',None,'dialog')
        self.assertEqual(fourth['status'],'active')
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM training_sessions'),4)

    async def test_no_daily_count_limit_in_both_modes(self):
        for mode in ('dialog','drill'):
            for _ in range(4):
                session=await training.start(self.pool,self.actor,'support',None,mode)
                self.assertEqual(session['status'],'active')
                await training.turn(self.pool,session,stop=True)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM training_sessions'),8)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM training_sessions WHERE status='active'"),0)

    async def test_repeat_requires_completed_session_and_ignores_later_abandoned(self):
        abandoned=await training.start(self.pool,self.actor,'support',None,'dialog')
        await training.turn(self.pool,abandoned,stop=True)
        with self.assertRaises(training.TrainingLimit):
            await training.pick(self.pool,self.actor,'repeat')
        completed=await training.start(self.pool,self.actor,'expensive',None,'drill')
        await training.turn(self.pool,completed,'С чем сравниваете стоимость?')
        await training.turn(self.pool,completed,stop=True)
        later=await training.start(self.pool,self.actor,'support',None,'dialog')
        await training.turn(self.pool,later,stop=True)
        changed=await training.pick(self.pool,self.actor,'repeat')
        self.assertEqual(changed['id'],'expensive')
        self.assertEqual(changed['repeat_number'],1)

    async def test_course_training_disabled_blocks_new_paid_turn(self):
        import training_simulator as old
        session=await training.start(self.pool,self.actor,'owner_minute',None,'dialog')
        fake=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()))
        with patch('methodology.training.training_enabled',return_value=False),patch.object(old.claude,'with_options',return_value=fake):
            result=await training.turn(self.pool,session,'Здравствуйте')
        self.assertIn('выключена',result.note)
        fake.messages.create.assert_not_awaited()

    async def test_course_drill_uses_same_evaluator_with_contextual_results(self):
        import handlers.evaluate_course as handler
        session=await training.start(self.pool,self.actor,'expensive',None,'drill')
        await training.turn(self.pool,session,'С чем сравниваете стоимость?')
        await training.turn(self.pool,session,stop=True)
        task=dict(await self.pool.fetchrow('SELECT * FROM tasks LIMIT 1'))
        raw=response();raw['classification']['evidence']=['С чем сравниваете стоимость?']
        raw['exercise_results']=[{'exercise':1,'criterion':'objection_response','status':'passed','evidence':'С чем сравниваете стоимость?','reason':'Уточнение сравнения','say_instead':''}]
        bot=SimpleNamespace(send_message=AsyncMock(),session=SimpleNamespace(close=AsyncMock()))
        with patch.object(transport,'request',AsyncMock(return_value=(json.dumps(raw),3))),patch.object(handler,'Bot',return_value=bot):
            await self.handler(self.pool,task)
        saved=json.loads(await self.pool.fetchval('SELECT result FROM methodology_evaluations'))
        self.assertEqual(saved['exercise_results'][0]['status'],'passed')
        self.assertIsNone(saved['quality']['score'])

    async def test_original_queue_progresses_during_slow_course_request(self):
        import queue_runner as queue
        await jobs.enqueue(self.pool,1,'call',self.call_id,TRANSCRIPT)
        legacy_id=await self.pool.fetchval("INSERT INTO tasks(type,client_id,input) VALUES('legacy_test',1,'{}') RETURNING id")
        legacy=AsyncMock(return_value={'ok':True})
        entered=asyncio.Event();release=asyncio.Event()
        async def reply(*args):
            entered.set();await release.wait()
            return json.dumps(response()),5
        queue._REGISTRY['legacy_test']=legacy
        try:
            with patch.object(transport,'request',AsyncMock(side_effect=reply)):
                course=asyncio.create_task(queue.run_once(self.pool,['evaluate_course']))
                await entered.wait()
                self.assertTrue(await queue.run_once(self.pool,['legacy_test']))
                self.assertEqual(await self.pool.fetchval('SELECT status FROM tasks WHERE id=$1',legacy_id),'done')
                self.assertFalse(course.done())
                release.set();await course
        finally:
            queue._REGISTRY.pop('legacy_test')

    async def test_assignment_consumed_only_with_successful_start(self):
        aid=await self.pool.fetchval("INSERT INTO pending_train_assignments(client_id,extension) VALUES(1,'101') RETURNING id")
        existing=await training.start(self.pool,self.actor,'owner_minute',None,'dialog')
        with self.assertRaises(training.TrainingLimit):
            await training.start(self.pool,self.actor,'support','head','dialog',aid)
        self.assertFalse(await self.pool.fetchval('SELECT consumed FROM pending_train_assignments WHERE id=$1',aid))
        await training.turn(self.pool,existing,stop=True)
        started=await training.start(self.pool,self.actor,'support','head','dialog',aid)
        self.assertTrue(await self.pool.fetchval('SELECT consumed FROM pending_train_assignments WHERE id=$1',aid))
        self.assertEqual(started['status'],'active')

    async def test_assignment_of_other_tenant_cannot_start(self):
        aid=await self.pool.fetchval("INSERT INTO pending_train_assignments(client_id,extension) VALUES(2,'201') RETURNING id")
        with self.assertRaises(training.TrainingLimit):
            await training.start(self.pool,self.actor,'support','head','dialog',aid)
        self.assertFalse(await self.pool.fetchval('SELECT consumed FROM pending_train_assignments WHERE id=$1',aid))
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM training_sessions'),0)

    async def test_automatic_digest_cannot_use_shadow_observations(self):
        import rop_agent
        self.assertNotIn('Методика курса работает параллельно',rop_agent._system_prompt('2026-10-01','UTC'))
        self.assertIn('Методика курса работает параллельно',rop_agent._system_prompt('2026-10-01','UTC',allow_course=True))
        with self.assertRaises(Forbidden):
            await rop_agent._dispatch_tool(self.pool,self.actor,'get_course_stats',{'period':{'start':'2026-10-01','end':'2026-10-02'}},set())

    async def test_tts_known_cost_survives_telegram_failure(self):
        import training_bot as bot
        import training_simulator as old
        session=await training.start(self.pool,self.actor,'owner_minute',None,'dialog')
        message=SimpleNamespace(answer=AsyncMock(side_effect=RuntimeError('text delivery failed')),
                                chat=SimpleNamespace(id=123),bot=SimpleNamespace(send_voice=AsyncMock(side_effect=RuntimeError('voice delivery failed'))))
        with patch.object(bot,'PG_POOL',self.pool),patch.object(bot.tts,'synthesize_ogg',AsyncMock(return_value=(b'synthetic_audio',.05))):
            with self.assertRaises(RuntimeError):
                await bot._send_training_reply(message,old.TurnResult('Слушаю.',False),session['id'])
        self.assertEqual(float(await self.pool.fetchval('SELECT cost_usd FROM training_sessions WHERE id=$1',session['id'])),.05)
