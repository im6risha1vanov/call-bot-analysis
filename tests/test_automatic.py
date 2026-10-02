"""Synthetic call pipeline and callback tests; no live providers or production DB."""
import asyncio
from datetime import datetime,timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from tests import test_comparison as fixtures
from tests.test_methodology import TRANSCRIPT,response
from tests.test_combined import combined_response,ValidHTML
from automatic import jobs,ui
from combined import evaluation,render
from comparison import transport
from comparison.store import decode
from reports import detail_button
from tools import Actor,Forbidden
from queue_runner import RetryLater
from aiogram import Bot,Router
from aiogram.types import CallbackQuery,User,Message,Chat


class AutomaticMenuTests(unittest.TestCase):
    def test_button_order_and_ready_preserve_all_three_variants(self):
        for ready in (False,True):
            kb=detail_button(123456789123,source='astra',ready=ready)
            self.assertEqual([r[0].callback_data for r in kb.inline_keyboard],['detail:astra:123456789123','av:c:123456789123','av:b:123456789123'])
            self.assertTrue(all(len(b.callback_data.encode())<=64 for r in kb.inline_keyboard for b in r))
        self.assertEqual(len(detail_button(1,source='pg').inline_keyboard),1)


@unittest.skipUnless(os.environ.get('METHODOLOGY_TEST_DATABASE_URL'),'Requires isolated PostgreSQL')
class AutomaticPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ComparisonTests.asyncSetUp(self)
        await self.pool.execute('TRUNCATE astra_daily_spend')
        root=Path(__file__).resolve().parents[1]
        await self.pool.execute((root/'migrations/001_course_methodology.sql').read_text())
        await self.pool.execute((root/'migrations/004_automatic_call_variants.sql').read_text())
        await self.pool.execute("""ALTER TABLE clients ADD COLUMN IF NOT EXISTS vpbx_api_key_enc TEXT;
          ALTER TABLE clients ADD COLUMN IF NOT EXISTS vpbx_api_salt_enc TEXT;
          ALTER TABLE employees ADD COLUMN IF NOT EXISTS id BIGSERIAL;
          ALTER TABLE calls ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();
          ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_id TEXT;
          ALTER TABLE astra_analysis ADD COLUMN IF NOT EXISTS short_report JSONB;
          ALTER TABLE astra_analysis ADD COLUMN IF NOT EXISTS detailed_report JSONB;
          ALTER TABLE astra_analysis ADD COLUMN IF NOT EXISTS detailed_report_requested_at TIMESTAMPTZ;
          ALTER TABLE astra_analysis ADD COLUMN IF NOT EXISTS cost_units NUMERIC DEFAULT 0;
          ALTER TABLE astra_analysis ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();
          ALTER TABLE astra_analysis ADD COLUMN IF NOT EXISTS immediate_sent_head BOOLEAN DEFAULT false;
          ALTER TABLE astra_analysis ADD COLUMN IF NOT EXISTS immediate_sent_manager BOOLEAN DEFAULT false;""")
        self.call_id=self.first
        self.meta={'call_id':self.call_id,'manager':'Первый','duration_seconds':90,'call_started_at':'2026-10-01T12:00:00+00:00','timezone':'Europe/Moscow','phone':'+*******1122'}
        self.requests=[]

    async def asyncTearDown(self):
        await self.pool.execute('TRUNCATE astra_daily_spend')
        await self.pool.close()
    add_call=fixtures.ComparisonTests.add_call

    async def enqueue(self):
        return await jobs.enqueue(self.pool,1,self.call_id,TRANSCRIPT,self.meta)

    async def task(self):
        return await self.pool.fetchrow("SELECT * FROM tasks WHERE type='automatic_combined_call' AND input->>'call_id'=$1",str(self.call_id))

    async def fake(self,prompt,user,settings):
        self.requests.append((prompt,user,settings))
        return json.dumps(combined_response()),12,{'input_tokens':1,'output_tokens':1}

    async def complete(self):
        await self.enqueue()
        with patch.object(transport,'request',self.fake):
            await jobs.run(self.pool,await self.task())

    async def test_no_backfill_deduplication_and_concurrent_execution(self):
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM automatic_call_reports'),0)
        results=await asyncio.gather(*[self.enqueue() for _ in range(4)])
        self.assertEqual(sum(results),1)
        task=await self.task()
        async def slow(*args):await asyncio.sleep(.03);return await self.fake(*args)
        before=await self.pool.fetch('SELECT * FROM astra_analysis ORDER BY call_id')
        with patch.object(transport,'request',slow):
            results=await asyncio.gather(jobs.run(self.pool,task),jobs.run(self.pool,task),return_exceptions=True)
        self.assertEqual(len(self.requests),1)
        self.assertTrue(any(isinstance(r,RetryLater) for r in results))
        self.assertEqual(before,await self.pool.fetch('SELECT * FROM astra_analysis ORDER BY call_id'))
        self.assertEqual(float(await self.pool.fetchval('SELECT sum(spent_units) FROM automatic_call_spend')),12)
        with patch.object(transport,'request',AsyncMock()) as paid:
            await jobs.run(self.pool,task);await self.enqueue()
        paid.assert_not_awaited()

    async def test_receipt_resumes_validation_and_accounts_once(self):
        await self.enqueue();row=await jobs.get(self.pool,self.actor,self.call_id)
        client=await self.pool.fetchrow('SELECT * FROM clients WHERE id=1')
        text=json.dumps(combined_response())
        await jobs.receipt(self.pool,row,client,text,12,{})
        await jobs.receipt(self.pool,row,client,text,12,{})
        with patch.object(transport,'request',AsyncMock()) as paid:
            await jobs.run(self.pool,await self.task())
        paid.assert_not_awaited()
        self.assertEqual((await jobs.get(self.pool,self.actor,self.call_id))['status'],'complete')
        self.assertEqual(float(await self.pool.fetchval('SELECT sum(spent_units) FROM automatic_call_spend')),12)

    async def test_unknown_provider_failure_and_restart_never_repeat_charge(self):
        from handlers.automatic_combined_call import recover_automatic_combined
        await self.enqueue();task=await self.task()
        with patch.object(transport,'request',AsyncMock(side_effect=transport.RequestFailure(502))) as paid:
            await jobs.run(self.pool,task);await jobs.run(self.pool,task)
        paid.assert_awaited_once()
        self.assertEqual((await jobs.get(self.pool,self.actor,self.call_id))['status'],'uncertain')
        await self.pool.execute("UPDATE automatic_call_reports SET status='processing',error=NULL")
        await recover_automatic_combined(self.pool)
        with patch.object(transport,'request',AsyncMock()) as paid:
            await jobs.run(self.pool,task)
        paid.assert_not_awaited()

    async def test_invalid_json_keeps_paid_receipt(self):
        await self.enqueue()
        with patch.object(transport,'request',AsyncMock(return_value=('{broken',13,{}))) as paid:
            await jobs.run(self.pool,await self.task());await jobs.run(self.pool,await self.task())
        paid.assert_awaited_once()
        row=await jobs.get(self.pool,self.actor,self.call_id)
        self.assertEqual(row['status'],'failed');self.assertEqual(row['response_text'],'{broken')
        self.assertEqual(float(await self.pool.fetchval('SELECT sum(spent_units) FROM automatic_call_spend')),13)

    async def test_disabled_client_or_mode_prevents_payment(self):
        await self.enqueue();task=await self.task()
        await self.pool.execute('UPDATE clients SET processing_enabled=false WHERE id=1')
        with patch.object(transport,'request',AsyncMock()) as paid,self.assertRaises(RetryLater):
            await jobs.run(self.pool,task)
        paid.assert_not_awaited()
        await self.pool.execute('UPDATE clients SET processing_enabled=true WHERE id=1')
        with patch.object(jobs,'enabled',return_value=False),patch.object(transport,'request',AsyncMock()) as paid,self.assertRaises(RetryLater):
            await jobs.run(self.pool,task)
        paid.assert_not_awaited()

    async def test_all_variants_share_existing_daily_budget(self):
        await self.enqueue()
        client=await self.pool.fetchrow('SELECT * FROM clients WHERE id=1')
        from zoneinfo import ZoneInfo
        day=datetime.now(ZoneInfo(client['timezone'])).date()
        await self.pool.execute('INSERT INTO astra_daily_spend VALUES(1,$1,1000000)',day)
        await self.pool.execute('INSERT INTO methodology_daily_spend VALUES(1,$1,999999)',day)
        await self.pool.execute('INSERT INTO automatic_call_spend VALUES(1,$1,1)',day)
        self.assertTrue(await jobs.over_budget(self.pool,client))
        with patch.object(transport,'request',AsyncMock()) as paid,self.assertRaises(RetryLater):
            await jobs.run(self.pool,await self.task())
        paid.assert_not_awaited()
        import handlers.analyze_call as original
        self.assertTrue(await original._over_daily_limit(self.pool,client))
        from methodology import jobs as course_jobs,transport as course_transport
        import handlers.evaluate_course as course
        await course_jobs.enqueue(self.pool,1,'call',self.call_id,TRANSCRIPT)
        task=await self.pool.fetchrow("SELECT * FROM tasks WHERE type='evaluate_course'")
        with patch.object(course_transport,'request',AsyncMock()) as paid,self.assertRaises(RetryLater):
            await course.evaluate_course(self.pool,task)
        paid.assert_not_awaited()

    async def test_pinned_input_and_current_transcript_guard(self):
        await self.enqueue();task=await self.task()
        await self.pool.execute("UPDATE calls SET transcript='changed' WHERE id=$1",self.call_id)
        with patch.object(transport,'request',AsyncMock()) as paid:
            self.assertIn('skipped',await jobs.run(self.pool,task))
        paid.assert_not_awaited();self.assertIsNone(await jobs.get(self.pool,self.actor,self.call_id))
        await self.pool.execute('UPDATE calls SET transcript=$2 WHERE id=$1',self.call_id,TRANSCRIPT)
        await self.pool.execute("UPDATE automatic_call_reports SET prompt='tampered'")
        with patch.object(transport,'request',AsyncMock()) as paid,self.assertRaises(ValueError):
            await jobs.run(self.pool,task)
        paid.assert_not_awaited()

    async def test_tenant_and_manager_access(self):
        await self.complete()
        for actor in (None,self.other,Actor(124,1,'manager','102')):
            with self.assertRaises(Forbidden):await jobs.get(self.pool,actor,self.call_id)
        for actor in (self.actor,Actor(901,1,'head','head'),Actor(123,1,'manager','101')):
            self.assertIsNotNone(await jobs.get(self.pool,actor,self.call_id))

    async def test_callbacks_current_user_stale_and_foreign_buttons_no_llm(self):
        await self.complete();router=Router();ui.install(router,lambda:self.pool)
        telegram=Bot('123456:synthetic_test_token')
        async def click(user,data,chat_id=None):
            message=Message(message_id=1,date=datetime.now(timezone.utc),chat=Chat(id=chat_id or user,type='private'),from_user=User(id=777,is_bot=True,first_name='Bot'),text='Report')
            cq=CallbackQuery(id='fake',from_user=User(id=user,is_bot=False,first_name='Person'),chat_instance='fake',data=data,message=message).as_(telegram)
            with patch.object(CallbackQuery,'answer',AsyncMock()) as ack,patch.object(Bot,'send_message',AsyncMock()) as send,patch.object(transport,'request',AsyncMock()) as paid:
                await router.propagate_event('callback_query',cq);ack.assert_awaited_once();paid.assert_not_awaited();return send
        for user in (900,901,123):
            sent=await click(user,f'av:b:{self.call_id}')
            self.assertIn('Объединённый разбор',sent.await_args.args[1])
            parser=ValidHTML();parser.feed(sent.await_args.args[1]);self.assertEqual(parser.stack,[])
            self.assertLessEqual(render.units(sent.await_args.args[1]),4096)
            self.assertEqual(sent.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data,f'av:d:{self.call_id}')
        self.assertIn('Критерии',(await click(900,f'av:d:{self.call_id}')).await_args.args[1])
        for user,data,chat in ((124,f'av:b:{self.call_id}',None),(902,f'av:b:{self.call_id}',None),(900,'av:bad:1',None),(900,f'av:b:{self.call_id}',901),(998,f'av:b:{self.call_id}',None),(900,'av:b:999999',None)):
            self.assertNotIn('<b>Объединённый разбор',(await click(user,data,chat)).await_args.args[1])
        self.assertIn('Дополнительного разбора',(await click(900,f'av:b:{self.second}')).await_args.args[1])
        await self.pool.execute("UPDATE automatic_call_reports SET status='pending',result=NULL")
        self.assertIn('готовится',(await click(900,f'av:b:{self.call_id}')).await_args.args[1])
        await self.pool.execute("UPDATE automatic_call_reports SET status='uncertain'")
        self.assertIn('ошибки',(await click(900,f'av:b:{self.call_id}')).await_args.args[1])
        await telegram.session.close()

    async def test_original_pipeline_unchanged_message_routes_and_two_jobs(self):
        import handlers.analyze_call as original
        call=await self.pool.fetchrow('SELECT * FROM calls WHERE id=$1',self.call_id)
        expected={'result':'Как раньше','good':[{'text':'Ясный вход'}],'weak':[]}
        fakebot=SimpleNamespace(send_message=AsyncMock())
        score=AsyncMock(return_value=({'call_cut_short':False},80,'❌',[],5))
        with patch.object(original,'decrypt',return_value='synthetic'),patch.object(original.mango_client,'fetch_recording',AsyncMock(return_value=b'fake')),patch.object(original,'transcribe_bytes',AsyncMock(return_value=(TRANSCRIPT,90))),patch.object(original,'score_call',score),patch.object(original,'short_report_call',AsyncMock(return_value=(expected,2))),patch.object(original,'_bot',fakebot):
            await original.analyze_call(self.pool,{'id':999,'input':json.dumps({'call_id':self.call_id})})
        score.assert_awaited_once()
        self.assertEqual([c.args[0] for c in fakebot.send_message.await_args_list],[123,900,901])
        for sent in fakebot.send_message.await_args_list:
            self.assertIn('Как раньше',sent.args[1])
            self.assertEqual(len(sent.kwargs['reply_markup'].inline_keyboard),3)
        types=await self.pool.fetch('SELECT type FROM tasks ORDER BY type')
        self.assertEqual([r['type'] for r in types],['automatic_combined_call','evaluate_course'])
        saved=await self.pool.fetchrow('SELECT * FROM astra_analysis WHERE call_id=$1',self.call_id)
        self.assertEqual(saved['score'],80);self.assertEqual(saved['level'],'❌')
        self.assertEqual(decode(saved['short_report']),expected)
        self.assertEqual(float(await self.pool.fetchval('SELECT sum(spent_units) FROM astra_daily_spend')),7)

    async def test_course_call_uses_overall_budget_instead_of_trial_cap(self):
        from methodology import jobs as course_jobs,transport as course_transport
        import handlers.evaluate_course as course
        client=await self.pool.fetchrow('SELECT * FROM clients WHERE id=1')
        from zoneinfo import ZoneInfo
        day=datetime.now(ZoneInfo(client['timezone'])).date()
        await self.pool.execute('INSERT INTO methodology_daily_spend VALUES(1,$1,218725)',day)
        await course_jobs.enqueue(self.pool,1,'call',self.call_id,TRANSCRIPT)
        task=await self.pool.fetchrow("SELECT * FROM tasks WHERE type='evaluate_course'")
        with patch.object(course_transport,'request',AsyncMock(return_value=(json.dumps(response()),3))) as paid:
            await course.evaluate_course(self.pool,task)
        paid.assert_awaited_once()
        self.assertEqual(await self.pool.fetchval('SELECT status FROM methodology_evaluations'),'complete')

    async def test_enqueue_failure_does_not_block_message_or_repeat_original_payment(self):
        import handlers.analyze_call as original
        fakebot=SimpleNamespace(send_message=AsyncMock())
        expected={'result':'Как раньше','good':[],'weak':[]}
        fake_score=AsyncMock(return_value=({'call_cut_short':False},80,'❌',[],5))
        task={'id':999,'input':json.dumps({'call_id':self.call_id})}
        with patch.object(original,'decrypt',return_value='synthetic'),patch.object(original.mango_client,'fetch_recording',AsyncMock(return_value=b'fake')),patch.object(original,'transcribe_bytes',AsyncMock(return_value=(TRANSCRIPT,90))),patch.object(original,'score_call',fake_score),patch.object(original,'short_report_call',AsyncMock(return_value=(expected,2))),patch.object(original,'_bot',fakebot),patch.object(jobs,'enqueue',AsyncMock(side_effect=RuntimeError('synthetic sidecar DB failure'))):
            with self.assertRaises(RetryLater):await original.analyze_call(self.pool,task)
        self.assertEqual(fakebot.send_message.await_count,3)
        self.assertEqual(await self.pool.fetchval('SELECT status FROM astra_analysis WHERE call_id=$1',self.call_id),'analyzed')
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM tasks'),0)
        with patch.object(original,'score_call',AsyncMock()) as paid,patch.object(original.mango_client,'fetch_recording',AsyncMock()) as download,patch.object(original,'_bot',fakebot):
            self.assertTrue((await original.analyze_call(self.pool,task))['cached'])
        paid.assert_not_awaited();download.assert_not_awaited()
        self.assertEqual(fakebot.send_message.await_count,3)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM tasks'),2)
        self.assertEqual(float(await self.pool.fetchval('SELECT sum(spent_units) FROM astra_daily_spend')),7)

    async def test_original_queue_progresses_during_combined_request(self):
        import queue_runner as queue
        import handlers.automatic_combined_call
        await self.enqueue()
        legacy_id=await self.pool.fetchval("INSERT INTO tasks(type,client_id,input) VALUES('legacy_auto_test',1,'{}') RETURNING id")
        entered=asyncio.Event();release=asyncio.Event()
        async def slow(*args):
            entered.set();await release.wait();return await self.fake(*args)
        queue._REGISTRY['legacy_auto_test']=AsyncMock(return_value={'ok':True})
        try:
            with patch.object(transport,'request',slow):
                sidecar=asyncio.create_task(queue.run_once(self.pool,['automatic_combined_call']))
                await entered.wait()
                self.assertTrue(await queue.run_once(self.pool,['legacy_auto_test']))
                self.assertEqual(await self.pool.fetchval('SELECT status FROM tasks WHERE id=$1',legacy_id),'done')
                self.assertFalse(sidecar.done());release.set();await sidecar
        finally:
            release.set();queue._REGISTRY.pop('legacy_auto_test')

    async def test_no_verdict_no_message_and_no_combined_job(self):
        import handlers.analyze_call as original
        fakebot=SimpleNamespace(send_message=AsyncMock())
        with patch.object(original,'decrypt',return_value='synthetic'),patch.object(original.mango_client,'fetch_recording',AsyncMock(return_value=b'fake')),patch.object(original,'transcribe_bytes',AsyncMock(return_value=(TRANSCRIPT,90))),patch.object(original,'score_call',AsyncMock(return_value=({'call_cut_short':True},None,None,[],5))),patch.object(original,'short_report_call',AsyncMock(return_value=({},2))),patch.object(original,'_bot',fakebot):
            await original.analyze_call(self.pool,{'id':999,'input':json.dumps({'call_id':self.call_id})})
        fakebot.send_message.assert_not_awaited()
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM automatic_call_reports'),0)

    async def test_course_callback_and_owner_original_button_preserve_other_buttons(self):
        from methodology import jobs as course_jobs,transport as course_transport
        import handlers.evaluate_course as course
        await course_jobs.enqueue(self.pool,1,'call',self.call_id,TRANSCRIPT)
        with patch.object(course_transport,'request',AsyncMock(return_value=(json.dumps(response()),3))):
            await course.evaluate_course(self.pool,await self.pool.fetchrow("SELECT * FROM tasks WHERE type='evaluate_course'"))
        router=Router();ui.install(router,lambda:self.pool)
        telegram=Bot('123456:synthetic_test_token')
        message=Message(message_id=1,date=datetime.now(timezone.utc),chat=Chat(id=900,type='private'),from_user=User(id=777,is_bot=True,first_name='Bot'),text='Report')
        cq=CallbackQuery(id='fake',from_user=User(id=900,is_bot=False,first_name='Owner'),chat_instance='fake',data=f'av:c:{self.call_id}',message=message).as_(telegram)
        with patch.object(CallbackQuery,'answer',AsyncMock()) as ack,patch.object(Bot,'send_message',AsyncMock()) as send:
            await router.propagate_event('callback_query',cq);ack.assert_awaited_once()
        self.assertIn('Разбор по методике курса',send.await_args.args[1]);self.assertIsNone(send.await_args.kwargs['parse_mode'])
        import demo_bot
        cq=cq.model_copy(update={'data':f'detail:astra:{self.call_id}'}).as_(telegram)
        with patch.object(demo_bot,'PG_POOL',self.pool),patch.object(demo_bot,'review_call',AsyncMock(return_value=(fixtures.review(),2))) as review,patch.object(CallbackQuery,'answer',AsyncMock()) as ack,patch.object(Message,'edit_reply_markup',AsyncMock()) as edit,patch.object(demo_bot,'send_chunks',AsyncMock()) as send:
            await demo_bot.detail_callback(cq)
        ack.assert_awaited_once();review.assert_awaited_once();send.assert_awaited_once()
        self.assertEqual(len(edit.await_args.kwargs['reply_markup'].inline_keyboard),3)
        await telegram.session.close()
