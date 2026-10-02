import asyncio
import copy
from html.parser import HTMLParser
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
# Imports set synthetic environment; never load the production .env in tests.
from tests import test_comparison as comparison_tests
from tests.test_methodology import response,TRANSCRIPT
from comparison import transport
from comparison.store import decode
from tools import Actor,Forbidden
from combined import evaluation,render,jobs,ui
from aiogram import Bot,Router
from aiogram.types import CallbackQuery,User,Message,Chat
from datetime import datetime,timezone


def combined_response():
    raw=response()
    raw['criteria']['next_step']={'status':'failed','evidence':'Да, отправьте КП','reason':'Срок не подтверждён'}
    raw['readable_report']={
      'summary':'Обсудили предложение и продолжение разговора.',
      'strengths':[{'criterion':'clarity','quote':'обсуждаем ГЦК','what':'Обозначили продукт','why':'Клиент понял тему'}],
      'moments':[{'criterion':'next_step','quote':'Да, отправьте КП','reaction_quote':'Да, согласен.',
                  'what':'Не уточнили срок','why':'Договорённость нуждается в уточнении','say_instead':'Когда удобно обсудить предложение?'}],
      'main_miss':{'criterion':'next_step','quote':'Да, отправьте КП','explanation':'Проверить конкретность продолжения'},
      'exercise':'Перед следующим разговором подготовьте один вопрос о сроке.',
      'next_focus':'Уточните срок и дождитесь согласия.',
      'leader':{'cause':'manager','quotes':['Да, отправьте КП'],'reason':'Нужен навык согласования продолжения','action':'Отработайте вопрос о сроке.'},
      'limits':[]}
    return raw


def grade(raw=None):
    return evaluation.evaluate(raw or combined_response(),TRANSCRIPT,config={'classification_min_confidence':.75},catalog={'version':'test','records':[]})

META={'call_id':123,'manager':'Сотрудник','call_started_at':'2026-10-01T17:55:00+03:00',
      'duration_seconds':61,'timezone':'Europe/Moscow','phone':'+*******1122'}


class ValidHTML(HTMLParser):
    def __init__(self):super().__init__();self.stack=[]
    def handle_starttag(self,tag,attrs):
        if tag not in {'b','i'}:raise AssertionError('Unexpected markup')
        self.stack.append(tag)
    def handle_endtag(self,tag):
        if not self.stack or self.stack.pop()!=tag:raise AssertionError('Broken HTML')


class CombinedUnitTests(unittest.TestCase):
    def test_complete_combined_prompt_preserves_stage_and_narrative_rules(self):
        evaluation.verify()
        p=evaluation.prompt()
        for phrase in ('Краткая отраслевая зацепка','readable_report','reaction_quote','main_miss','Числовой балл','версия'):
            self.assertIn(phrase,p)

    def test_unsupported_quotes_and_mismatched_criterion_are_removed(self):
        raw=combined_response()
        raw['readable_report']['strengths'][0]['quote']='несуществующая реплика'
        raw['readable_report']['moments'][0]['criterion']='brush_off_handled'
        raw['readable_report']['main_miss']['criterion']='clarity'  # passed cannot support an accusation
        raw['readable_report']['leader']['quotes']=['выдуманная причина']
        result=grade(raw)['readable_report']
        self.assertEqual(result['strengths'],[]);self.assertEqual(result['moments'],[])
        self.assertIsNone(result['main_miss']);self.assertEqual(result['leader']['cause'],'insufficient_data')
        self.assertTrue(result['validation_warnings'])

    def test_fake_reaction_not_presented_as_client_words(self):
        raw=combined_response();raw['readable_report']['moments'][0]['reaction_quote']='Клиент сильно рассердился'
        result=grade(raw)
        self.assertIsNone(result['readable_report']['moments'][0]['reaction_quote'])
        self.assertNotIn('Клиент сильно рассердился','\n'.join(render.cards(result,META)))

    def test_unknown_product_only_general_criteria_not_forced_meeting(self):
        raw=combined_response();raw['classification'].update(product='unknown',confidence=.4)
        raw['readable_report']['moments'][0]['criterion']='economics'
        result=grade(raw)
        self.assertFalse(result['narrow_profile_applied'])
        self.assertEqual(len(result['rows']),5)
        self.assertEqual(result['readable_report']['moments'],[])
        self.assertIsNone(result['quality']['score'])

    def test_all_roles_and_stages_keep_correct_applicability(self):
        from comparison.packages.course.methodology import profiles
        for role in profiles.ROLES-{'unknown'}:
            for stage in profiles.STAGES-{'unknown'}:
                raw=response(stage,role);raw['readable_report']=combined_response()['readable_report']
                result=grade(raw)
                self.assertEqual(set(r['key'] for r in result['rows']),set(profiles.criteria_for(role,stage,True)))

    def test_one_quote_not_duplicated_across_main_miss_and_moment(self):
        text='\n'.join(render.cards(grade(),META))
        self.assertEqual(text.count('Менеджер: «Да, отправьте КП»'),1)
        self.assertNotIn('readable_report',text);self.assertNotIn('criterion',text)
        self.assertNotIn('{',text);self.assertNotIn('СЛАБО',text)
        self.assertIn('🎯 Итог',text);self.assertIn('Можно сказать:',text)

    def test_html_escape_long_unicode_sections_split_with_no_data_loss(self):
        value='<b>&"😀' * 2000
        blocks=['<b>Тест</b>\n'+render.esc(value)]
        chunks=render.pack(blocks)
        self.assertTrue(len(chunks)>1)
        plain=[]
        for chunk in chunks:
            self.assertLessEqual(render.units(chunk),3800)
            parser=ValidHTML();parser.feed(chunk);self.assertEqual(parser.stack,[])
            import re,html
            plain.append(html.unescape(re.sub(r'</?(?:b|i)>','',chunk)))
        self.assertEqual(''.join(plain),'Тест\n'+value)

    def test_readable_report_validation_json_and_bounded_fields(self):
        for bad in (None,[],{'summary':False},{**combined_response()['readable_report'],'leader':{'cause':'fire_manager'}}):
            raw=combined_response();raw['readable_report']=bad
            with self.assertRaises(ValueError):grade(raw)
        raw=combined_response();raw['readable_report']['summary']='x'*1000
        self.assertLessEqual(len(grade(raw)['readable_report']['summary']),180)


# Do not inherit comparison test methods; borrow only guarded fixtures/helpers.
@unittest.skipUnless(os.environ.get('METHODOLOGY_TEST_DATABASE_URL'),'Requires isolated test database')
class CombinedPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await comparison_tests.ComparisonTests.asyncSetUp(self)
        await self.pool.execute((Path(__file__).resolve().parents[1]/'migrations/003_combined_analysis.sql').read_text())
        self.rid=await comparison_tests.ComparisonTests.complete(self)
        self.requests=[]
    asyncTearDown=comparison_tests.ComparisonTests.asyncTearDown
    add_call=comparison_tests.ComparisonTests.add_call
    fake=comparison_tests.ComparisonTests.fake

    async def combined_fake(self,prompt,user,settings):
        self.requests.append((prompt,user,settings))
        return json.dumps(combined_response(),ensure_ascii=False),13,{'input_tokens':1,'output_tokens':1}

    async def complete(self):
        await jobs.request(self.pool,self.actor,self.rid)
        with patch.object(transport,'request',self.combined_fake):
            await jobs.run(self.pool,self.rid,1)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM combined_reports WHERE status='complete'"),2)

    async def test_original_experiment_and_analysis_remain_unchanged(self):
        before=await self.pool.fetch('SELECT * FROM analysis_comparison_stages ORDER BY ordinal,version,stage')
        call_before=await self.pool.fetch('SELECT * FROM calls ORDER BY id')
        prod_before=await self.pool.fetch('SELECT * FROM astra_analysis ORDER BY call_id')
        await self.complete()
        self.assertEqual(before,await self.pool.fetch('SELECT * FROM analysis_comparison_stages ORDER BY ordinal,version,stage'))
        self.assertEqual(call_before,await self.pool.fetch('SELECT * FROM calls ORDER BY id'))
        self.assertEqual(prod_before,await self.pool.fetch('SELECT * FROM astra_analysis ORDER BY call_id'))
        self.assertEqual(len(self.requests),2)
        self.assertTrue(all(TRANSCRIPT in r[1] for r in self.requests))
        self.assertTrue(all(r[2]==self.requests[0][2] for r in self.requests))
        self.assertEqual(await self.pool.fetchval('SELECT revealed_at FROM analysis_comparisons'),None)

    async def test_double_request_and_worker_lock_no_extra_charge(self):
        requests=await asyncio.gather(*[jobs.request(self.pool,self.actor,self.rid) for _ in range(3)])
        self.assertEqual(sum(v[1] for v in requests),1)
        async def slow(*args):await asyncio.sleep(.01);return await self.combined_fake(*args)
        from queue_runner import RetryLater
        with patch.object(transport,'request',slow):
            outcomes=await asyncio.gather(jobs.run(self.pool,self.rid,1),jobs.run(self.pool,self.rid,1),return_exceptions=True)
        self.assertEqual(len(self.requests),2)
        self.assertTrue(any(isinstance(o,RetryLater) for o in outcomes))
        with patch.object(transport,'request',AsyncMock(side_effect=AssertionError('duplicate charge'))):
            await jobs.run(self.pool,self.rid,1)
        self.assertEqual(float(await self.pool.fetchval('SELECT sum(cost_units) FROM combined_reports')),26)

    async def test_received_json_restored_without_second_request(self):
        await jobs.request(self.pool,self.actor,self.rid)
        await self.pool.execute("UPDATE combined_reports SET status='received',response_text=$1,cost_units=13,usage='{}' WHERE ordinal=1",json.dumps(combined_response()))
        with patch.object(transport,'request',self.combined_fake):await jobs.run(self.pool,self.rid,1)
        self.assertEqual(len(self.requests),1)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM combined_reports WHERE status='complete'"),2)

    async def test_unknown_and_invalid_json_no_repeat_billing(self):
        await jobs.request(self.pool,self.actor,self.rid)
        with patch.object(transport,'request',AsyncMock(side_effect=transport.RequestFailure())) as fake:
            await jobs.run(self.pool,self.rid,1);await jobs.run(self.pool,self.rid,1)
        self.assertEqual(fake.await_count,2)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM combined_reports WHERE status='uncertain'"),2)
        await self.pool.execute("UPDATE combined_reports SET status='received',response_text='{bad',cost_units=12")
        with patch.object(transport,'request',AsyncMock()) as fake:
            await jobs.run(self.pool,self.rid,1);await jobs.run(self.pool,self.rid,1)
        fake.assert_not_awaited()
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM combined_reports WHERE status='failed'"),2)

    async def test_authorization_and_rebind_block_requests(self):
        for actor in (None,Actor(123,1,'manager','101'),self.other):
            with self.assertRaises(Forbidden):await jobs.request(self.pool,actor,self.rid)
        await jobs.request(self.pool,self.actor,self.rid)
        await self.pool.execute("UPDATE employees SET role='manager' WHERE telegram_user_id=900")
        with patch.object(transport,'request',AsyncMock()) as fake,self.assertRaises(Forbidden):
            await jobs.run(self.pool,self.rid,1)
        fake.assert_not_awaited()

    async def test_delivery_html_and_cached_reopen_without_model(self):
        await self.complete()
        await jobs.deliver(self.pool,self.telegram,self.actor,self.rid)
        n=self.telegram.send_message.await_count
        self.assertGreaterEqual(n,2)
        for call in self.telegram.send_message.await_args_list:
            self.assertEqual(call.kwargs['parse_mode'],'HTML')
            parser=ValidHTML();parser.feed(call.args[1]);self.assertEqual(parser.stack,[])
            self.assertLessEqual(render.units(call.args[1]),4096)
        await jobs.deliver(self.pool,self.telegram,self.actor,self.rid)
        self.assertEqual(self.telegram.send_message.await_count,n)
        await jobs.deliver(self.pool,self.telegram,self.actor,self.rid,reopen=True)
        self.assertEqual(self.telegram.send_message.await_count,n*2)
        self.assertEqual(len(self.requests),2)

    async def test_delivery_failure_retry_uses_receipts(self):
        await self.complete()
        self.telegram.send_message.side_effect=RuntimeError('Fake Telegram failure')
        with self.assertRaises(RuntimeError):await jobs.deliver(self.pool,self.telegram,self.actor,self.rid)
        self.telegram.send_message.side_effect=None
        with patch.object(transport,'request',AsyncMock()) as fake:
            await jobs.run(self.pool,self.rid,1)
            await jobs.deliver(self.pool,self.telegram,self.actor,self.rid)
        fake.assert_not_awaited()
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM combined_report_delivery WHERE status!='sent'"),0)

    async def test_restart_records_unknown_request_and_delivery(self):
        from handlers.combined_analysis import recover_combined
        await jobs.request(self.pool,self.actor,self.rid)
        await self.pool.execute("UPDATE combined_reports SET status='processing' WHERE ordinal=1")
        await self.pool.execute("INSERT INTO combined_report_delivery(comparison_id,ordinal,version,recipient,part,status) VALUES($1,1,$2,900,0,'sending')",self.rid,evaluation.VERSION)
        await recover_combined(self.pool)
        self.assertEqual(await self.pool.fetchval('SELECT status FROM combined_reports WHERE ordinal=1'),'uncertain')
        self.assertEqual(await self.pool.fetchval('SELECT status FROM combined_report_delivery'),'uncertain')
        with patch.object(transport,'request',self.combined_fake):await jobs.run(self.pool,self.rid,1)
        self.assertEqual(len(self.requests),1)

    async def test_callback_current_user_ownership_and_always_acked(self):
        await self.complete();router=Router();ui.install(router,lambda:self.pool)
        telegram=Bot('123456:synthetic_test_token')
        async def click(user,data,chat_id=None):
            message=Message(message_id=1,date=datetime.now(timezone.utc),chat=Chat(id=chat_id or user,type='private'),from_user=User(id=777,is_bot=True,first_name='Bot'),text='Report')
            callback=CallbackQuery(id='fake',from_user=User(id=user,is_bot=False,first_name='Person'),chat_instance='fake',data=data,message=message).as_(telegram)
            with patch.object(CallbackQuery,'answer',AsyncMock()) as ack,patch.object(Bot,'send_message',AsyncMock()) as send:
                await router.propagate_event('callback_query',callback);ack.assert_awaited_once();return send
        answer=await click(900,f'cb:d:{self.rid}:1')
        self.assertIn('Критерии',answer.await_args.args[1])
        for user,data,chat in ((123,f'cb:d:{self.rid}:1',None),(902,f'cb:d:{self.rid}:1',None),(900,'cb:bad:1:1',None),(900,f'cb:d:{self.rid}:1',901)):
            answer=await click(user,data,chat)
            self.assertNotIn('Критерии',answer.await_args.args[1])
        await telegram.session.close()
