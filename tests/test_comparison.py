"""Only synthetic transcripts and fake providers/Telegram; guarded temporary PostgreSQL."""
import asyncio
import base64
from datetime import datetime,timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
os.environ.setdefault('CVC_API_KEY','synthetic-test-key')
os.environ.setdefault('BOT_TOKEN','123456:synthetic_test_token')
os.environ.setdefault('TRAIN_BOT_TOKEN','123457:synthetic_test_token')
os.environ.setdefault('ANTHROPIC_API_KEY','synthetic-test-key')
os.environ.setdefault('ENCRYPTION_KEY',base64.urlsafe_b64encode(b'0'*32).decode())
import asyncpg
from aiogram import Bot,Router
from aiogram.types import Message,User,Chat,CallbackQuery
from comparison import store,engine,transport,registry,presentation,ui
from tools import Actor,Forbidden
from tests.test_methodology import TRANSCRIPT,response
DSN=os.environ.get('METHODOLOGY_TEST_DATABASE_URL')


def legacy_score():
    mod=registry.modules(registry.LEGACY)
    return {'criteria':{key:{'applicable':True,'passed':False,'evidence':'Диагностический вопрос не задан'} for key,_,_ in mod.CRITERIA},'call_cut_short':False,'meeting_booked':False,'signals':{}}


def short():
    return {'result':'Продолжение не согласовано','good':[{'time':None,'text':'нечем'}],'weak':[]}


def review():
    return {'headline':'Короткий разговор','for_head':{'narrative':'Проверка потребности','skill_gaps':[]},
            'for_manager':{'opening':'Есть над чем работать','did_well':[],'moments':[], 'key_mistake':'Не уточнён срок'}}


class PureTests(unittest.TestCase):
    def test_verified_complete_packages_and_corruption(self):
        manifest=registry.verify()
        self.assertIn('legacy/analysis.py',manifest[registry.LEGACY]['files'])
        self.assertIn('legacy/prompt_review.md',manifest[registry.LEGACY]['files'])
        self.assertIn('course/methodology/profiles.py',manifest[registry.COURSE]['files'])
        self.assertIn('course/prompts/course/review.md',manifest[registry.COURSE]['files'])
        with patch.object(registry,'sha',return_value='wrong'),self.assertRaises(ValueError):
            registry.verify()

    def test_shared_quote_validation_and_hypothetical_exclusion(self):
        self.assertEqual(presentation.quote_issues({'quote':'нет такой реплики','say_instead':'Придуманный пример'},TRANSCRIPT),['нет такой реплики'])
        self.assertEqual(presentation.quote_issues({'quote':TRANSCRIPT.splitlines()[0]},TRANSCRIPT),[])

    def test_callback_size_and_blind_names(self):
        kb=presentation.vote_keyboard(10**12,2,100)
        self.assertTrue(all(len(b.callback_data.encode())<=64 for row in kb.inline_keyboard for b in row))
        self.assertNotIn('legacy',str(kb));self.assertNotIn('course',str(kb))

    def test_all_settings_explicit_equal_and_seed_not_claimed(self):
        settings=transport.settings()
        self.assertEqual(settings['max_output_tokens'],6000)
        self.assertIsNone(settings['seed'])
        self.assertIn('legacy_verdict',settings)


@unittest.skipUnless(DSN,'Requires isolated test PostgreSQL')
class ComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool=await asyncpg.create_pool(DSN,min_size=1,max_size=8)
        self.assertTrue((await self.pool.fetchval('SELECT current_database()')).startswith('callbot_methodology_test_'))
        root=Path(__file__).resolve().parents[1]
        if not await self.pool.fetchval("SELECT to_regclass('clients') IS NOT NULL"):
            await self.pool.execute((root/'tests/bootstrap.sql').read_text())
        await self.pool.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS duration_seconds INTEGER DEFAULT 90; ALTER TABLE calls ADD COLUMN IF NOT EXISTS client_number TEXT; ALTER TABLE astra_analysis ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'analyzed'; ALTER TABLE astra_analysis ADD COLUMN IF NOT EXISTS analysis JSONB DEFAULT '{}'")
        await self.pool.execute((root/'migrations/002_analysis_comparison.sql').read_text())
        await self.pool.execute('TRUNCATE clients CASCADE')
        await self.pool.execute("INSERT INTO clients VALUES(1,'Europe/Moscow',true),(2,'UTC',true)")
        await self.pool.execute("INSERT INTO employees VALUES(1,'101',123,'manager','Первый'),(1,'102',124,'manager','Второй'),(1,'',900,'owner','Владелец'),(1,'head',901,'head','Руководитель'),(2,'201',223,'manager','Другой'),(2,'',902,'owner','Другой владелец')")
        self.actor=Actor(900,1,'owner',''); self.other=Actor(902,2,'owner','')
        self.first=await self.add_call('101','2026-10-01T12:00:00+00:00')
        self.second=await self.add_call('101','2026-10-01T13:00:00+00:00')
        self.different=await self.add_call('102','2026-10-01T11:00:00+00:00')
        self.telegram=SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(type='private')),send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),send_document=AsyncMock(return_value=SimpleNamespace(message_id=2)))
        self.requests=[]

    async def asyncTearDown(self):
        await self.pool.close()

    async def add_call(self,extension,when,*,status='analyzed',transcript=TRANSCRIPT,client=1,cut=False):
        cid=await self.pool.fetchval('INSERT INTO calls(client_id,extension,transcript,status,call_started_at,client_number) VALUES($1,$2,$3,$4,$5,$6) RETURNING id',client,extension,transcript,status,datetime.fromisoformat(when),'79990001122')
        await self.pool.execute("INSERT INTO astra_analysis(call_id,score,level,analysis) VALUES($1,87,'✅',$2::jsonb)",cid,json.dumps({'call_cut_short':cut}))
        return cid

    async def fake(self,prompt,user,params):
        self.requests.append((prompt,user,params))
        if 'СХЕМА ПРОФИЛЕЙ' in prompt:
            raw=response()
        elif 'for_manager' in prompt:
            raw=review()
        elif 'should_say' in prompt:
            raw=short()
        else:
            raw=legacy_score()
        return json.dumps(raw,ensure_ascii=False),10,{'input_tokens':1,'output_tokens':1}

    async def complete(self):
        rid,_=await store.create(self.pool,self.actor)
        with patch.object(transport,'request',self.fake):
            result=await engine.run(self.pool,rid)
        self.assertEqual(result['status'],'ready')
        return rid

    async def test_latest_distinct_managers_excludes_bad_and_other_client(self):
        await self.add_call('101','2026-10-02T12:00:00+00:00',cut=True)
        await self.add_call('101','2026-10-02T13:00:00+00:00',transcript=' ')
        await self.add_call('101','2026-10-02T14:00:00+00:00',status='failed')
        await self.add_call('201','2026-10-02T15:00:00+00:00',client=2)
        rows=await store.select_calls(self.pool,1)
        self.assertEqual([r['id'] for r in rows],[self.second,self.different])

    async def test_single_manager_and_single_call_fallback_stable_id_tie(self):
        await self.pool.execute('DELETE FROM astra_analysis WHERE call_id=$1',self.different)
        rows=await store.select_calls(self.pool,1)
        self.assertEqual([r['id'] for r in rows],[self.second,self.first])
        await self.pool.execute('DELETE FROM astra_analysis WHERE call_id=$1',self.first)
        self.assertEqual(len(await store.select_calls(self.pool,1)),1)
        tie=await self.add_call('101','2026-10-01T13:00:00+00:00')
        self.assertEqual((await store.select_calls(self.pool,1))[0]['id'],tie)

    async def test_double_launch_concurrent_no_duplicates_and_explicit_new(self):
        outcomes=await asyncio.gather(*[store.create(self.pool,self.actor,new=True) for _ in range(5)])
        self.assertEqual(len({o[0] for o in outcomes}),1)
        self.assertEqual(sum(o[1] for o in outcomes),1)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM tasks WHERE type='compare_analysis'"),1)
        await self.pool.execute("UPDATE analysis_comparisons SET status='ready'")
        self.assertFalse((await store.create(self.pool,self.actor))[1])
        self.assertTrue((await store.create(self.pool,self.actor,new=True))[1])

    async def test_same_input_params_pinned_stages_no_production_change_cached_restart(self):
        config=(Path(__file__).resolve().parents[1]/'methodology/config.json').read_bytes()
        old=await self.pool.fetch('SELECT * FROM astra_analysis ORDER BY call_id')
        rid=await self.complete()
        self.assertEqual(len(self.requests),8)  # legacy 3 + course 1 requests per call
        self.assertTrue(all(r[2]==self.requests[0][2] for r in self.requests))
        for _,user,_ in self.requests:
            self.assertIn(TRANSCRIPT,user)
            self.assertEqual(user.count(TRANSCRIPT),1)
        before=await self.pool.fetch('SELECT a_version,b_version,transcript_sha256 FROM analysis_comparison_calls WHERE comparison_id=$1 ORDER BY ordinal',rid)
        with patch.object(transport,'request',AsyncMock(side_effect=AssertionError('Unexpected paid retry'))):
            await engine.run(self.pool,rid)
        self.assertEqual(before,await self.pool.fetch('SELECT a_version,b_version,transcript_sha256 FROM analysis_comparison_calls WHERE comparison_id=$1 ORDER BY ordinal',rid))
        self.assertEqual(old,await self.pool.fetch('SELECT * FROM astra_analysis ORDER BY call_id'))
        self.assertEqual(config,(Path(__file__).resolve().parents[1]/'methodology/config.json').read_bytes())
        self.assertEqual(float(await self.pool.fetchval('SELECT sum(cost_units) FROM analysis_comparison_stages')),80)
        # Both methods receive the same metadata prefix, with no output from the other version.
        self.assertEqual(len({r[1].split('ТРАНСКРИПТ:')[0].split('КОНТЕКСТ',1)[-1] for r in self.requests}),2)

    async def test_transcript_is_snapshot_not_new_call_value(self):
        rid,_=await store.create(self.pool,self.actor)
        await self.pool.execute("UPDATE calls SET transcript='изменилось после запуска'")
        with patch.object(transport,'request',self.fake):
            self.assertEqual((await engine.run(self.pool,rid))['status'],'ready')
        self.assertTrue(all(TRANSCRIPT in r[1] for r in self.requests))

    async def test_authorization_before_billing_and_foreign_comparison(self):
        for actor in (None,Actor(123,1,'manager','101')):
            with self.assertRaises(Forbidden):
                await store.create(self.pool,actor)
        rid,_=await store.create(self.pool,self.actor)
        with self.assertRaises(Forbidden):
            await store.get(self.pool,self.other,rid)
        await self.pool.execute("UPDATE employees SET role='manager' WHERE telegram_user_id=900")
        fake=AsyncMock()
        with patch.object(transport,'request',fake),self.assertRaises(Forbidden):
            await engine.run(self.pool,rid)
        fake.assert_not_awaited()

    async def test_failure_unknown_request_is_not_repeated_other_variant_finishes(self):
        rid,_=await store.create(self.pool,self.actor)
        async def provider(prompt,user,params):
            if 'СХЕМА ПРОФИЛЕЙ' in prompt:
                raise transport.RequestFailure()
            return await self.fake(prompt,user,params)
        with patch.object(transport,'request',provider):
            self.assertEqual((await engine.run(self.pool,rid))['status'],'partial')
        self.assertEqual(len(self.requests),6)
        with patch.object(transport,'request',AsyncMock(side_effect=AssertionError('paid repeat'))):
            await engine.run(self.pool,rid)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM analysis_comparison_stages WHERE status='uncertain'"),2)

    async def test_restart_with_receipt_continues_missing_only(self):
        rid,_=await store.create(self.pool,self.actor)
        await self.pool.execute("UPDATE analysis_comparison_stages SET status='received',response_text=$2,cost_units=3 WHERE comparison_id=$1 AND ordinal=1 AND version=$3 AND stage='score'",rid,json.dumps(legacy_score()),registry.LEGACY)
        with patch.object(transport,'request',self.fake):
            self.assertEqual((await engine.run(self.pool,rid))['status'],'ready')
        self.assertEqual(len(self.requests),7)
        self.assertEqual(float(await self.pool.fetchval('SELECT sum(cost_units) FROM analysis_comparison_stages')),73)

    async def test_unknown_request_restored_no_second_charge(self):
        from handlers.compare_analysis import recover_comparisons
        rid,_=await store.create(self.pool,self.actor)
        await self.pool.execute("UPDATE analysis_comparison_stages SET status='processing' WHERE comparison_id=$1 AND ordinal=1 AND version=$2 AND stage='score'",rid,registry.COURSE)
        await recover_comparisons(self.pool)
        with patch.object(transport,'request',self.fake):
            self.assertEqual((await engine.run(self.pool,rid))['status'],'partial')
        self.assertEqual(len(self.requests),7)

    async def test_bad_json_receipt_not_rebilled(self):
        rid,_=await store.create(self.pool,self.actor)
        with patch.object(transport,'request',AsyncMock(return_value=('{bad',12,{}))) as fake:
            await engine.run(self.pool,rid);await engine.run(self.pool,rid)
        self.assertEqual(fake.await_count,4)
        self.assertEqual(float(await self.pool.fetchval('SELECT sum(cost_units) FROM analysis_comparison_stages')),48)

    async def test_parallel_workers_only_one_spends(self):
        rid,_=await store.create(self.pool,self.actor)
        async def slower(*args):
            await asyncio.sleep(.01)
            return await self.fake(*args)
        with patch.object(transport,'request',slower):
            outcomes=await asyncio.gather(engine.run(self.pool,rid),engine.run(self.pool,rid))
        self.assertEqual(len(self.requests),8)
        self.assertTrue(any(o.get('busy') for o in outcomes))

    async def test_votes_comment_optional_amend_reveal_and_foreign_actor(self):
        rid=await self.complete()
        with self.assertRaises(Forbidden):
            await store.vote(self.pool,Actor(901,1,'head','head'),rid,1,'a')
        with self.assertRaises(ValueError):
            await store.vote(self.pool,self.actor,rid,3,'a')
        await store.vote(self.pool,self.actor,rid,1,'a')
        self.assertEqual(await store.comment(self.pool,self.actor,'Полезный разбор'),rid)
        await store.vote(self.pool,self.actor,rid,1,'b')
        with self.assertRaises(ValueError):
            await store.reveal(self.pool,self.actor,rid)
        await store.vote(self.pool,self.actor,rid,2,'equal')
        self.assertIsNotNone(await presentation.completion_keyboard(self.pool,rid))
        await store.reveal(self.pool,self.actor,rid)
        with self.assertRaises(ValueError):
            await store.vote(self.pool,self.actor,rid,1,'a')
        self.assertIn('Полезный разбор',await presentation.summary(self.pool,self.actor,rid))
        self.assertIn('course',await presentation.summary(self.pool,self.actor,rid))

    async def test_delivery_persisted_blind_complete_attachments_and_reopening_free(self):
        rid=await self.complete()
        await presentation.send_results(self.pool,self.telegram,self.actor,rid)
        self.assertEqual(self.telegram.send_document.await_count,4)
        for call in self.telegram.send_document.await_args_list:
            doc=call.args[1]
            self.assertNotIn('course',doc.filename);self.assertNotIn('legacy',doc.filename)
            text=doc.data.decode();self.assertIn('ПОДРОБНЫЙ РАЗБОР',text)
            self.assertNotIn('по методике курса',text)
        await presentation.send_results(self.pool,self.telegram,self.actor,rid)
        self.assertEqual(self.telegram.send_document.await_count,4)
        await presentation.send_results(self.pool,self.telegram,self.actor,rid,reopen=True)
        self.assertEqual(self.telegram.send_document.await_count,8)
        self.assertEqual(len(self.requests),8)

    async def test_delivery_failure_resumes_only_unsent_without_model(self):
        rid=await self.complete()
        self.telegram.send_document.side_effect=RuntimeError('mock Telegram')
        with self.assertRaises(RuntimeError):
            await presentation.send_results(self.pool,self.telegram,self.actor,rid)
        self.telegram.send_document.side_effect=None
        await presentation.send_results(self.pool,self.telegram,self.actor,rid)
        self.assertEqual(self.telegram.send_message.await_count,5)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM analysis_comparison_delivery WHERE status='sent'"),9)

    async def test_callback_action_actor_not_bot_author_malformed_foreign_buttons_acked(self):
        rid=await self.complete()
        router=Router();ui.install(router,lambda:self.pool)
        telegram=Bot('123456:synthetic_test_token')
        async def click(user,data,chat_id=None):
            message=Message(message_id=1,date=datetime.now(timezone.utc),chat=Chat(id=chat_id or user,type='private'),from_user=User(id=777,is_bot=True,first_name='Bot'),text='catalog')
            callback=CallbackQuery(id='fake',from_user=User(id=user,is_bot=False,first_name='Person'),chat_instance='fake',data=data,message=message).as_(telegram)
            with patch.object(CallbackQuery,'answer',AsyncMock()) as ack,patch.object(Bot,'send_message',AsyncMock()) as send:
                await router.propagate_event('callback_query',callback)
                ack.assert_awaited_once();return send
        await click(900,f'ac:v:{rid}:1:a')
        self.assertEqual(await self.pool.fetchval('SELECT voter FROM analysis_comparison_votes'),900)
        await click(901,f'ac:v:{rid}:2:a')
        await click(900,f'ac:v:{rid}:2:a',901)
        await click(900,'ac:v:garbage')
        await click(900,f'ac:bad:{rid}')
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM analysis_comparison_votes'),1)
        await telegram.session.close()

    async def test_comment_routed_before_general_agent_no_llm(self):
        import demo_bot
        rid=await self.complete()
        await store.vote(self.pool,self.actor,rid,1,'a')
        message=SimpleNamespace(from_user=SimpleNamespace(id=900),text='Точный полезный комментарий',answer=AsyncMock())
        with patch.object(demo_bot,'PG_POOL',self.pool),patch.object(demo_bot.rop_agent,'answer',AsyncMock()) as llm:
            await demo_bot.rop_question(message)
        llm.assert_not_awaited()
        self.assertEqual(await self.pool.fetchval('SELECT comment FROM analysis_comparison_votes'),'Точный полезный комментарий')
