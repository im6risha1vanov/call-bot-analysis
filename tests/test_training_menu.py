"""Offline Telegram interactions: no production database or external requests."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import io
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault('CVC_API_KEY', 'synthetic-test-key')
os.environ.setdefault('BOT_TOKEN', '123456:synthetic_test_token')
os.environ.setdefault('TRAIN_BOT_TOKEN', '123457:synthetic_test_token')
os.environ.setdefault('ANTHROPIC_API_KEY', 'synthetic-test-key')
os.environ.setdefault('ENCRYPTION_KEY', base64.urlsafe_b64encode(b'0' * 32).decode())

from aiogram import Bot
from aiogram.filters import CommandObject
from aiogram.types import CallbackQuery, Chat, Message, User, Voice
import training_bot as bot
import training_menu as menu
from methodology import scenarios, training
from tools import Actor


class CatalogTests(unittest.TestCase):
    def test_persistent_keyboard_exact_layout(self):
        keyboard = menu.main_keyboard()
        self.assertEqual([[b.text for b in row] for row in keyboard.keyboard],
                         [list(menu.ACTIONS[n:n + 2]) for n in (0, 2, 4)])
        self.assertTrue(keyboard.resize_keyboard)
        if 'is_persistent' in type(keyboard).model_fields:
            self.assertTrue(keyboard.is_persistent)

    def test_entire_registry_both_modes_and_boundary_arrows(self):
        for mode in menu.MODE_NAMES:
            seen = []
            pages = (len(scenarios.SCENARIOS) + 7) // 8
            for page in range(pages):
                text, keyboard = menu.catalog(mode, page)
                self.assertIn(f'Страница {page + 1}/{pages}', text)
                self.assertLessEqual(len(text), 4096)
                buttons = [b for row in keyboard.inline_keyboard for b in row]
                choices = [b for b in buttons if b.callback_data.startswith('tm:r:')]
                self.assertLessEqual(len(choices), 8)
                self.assertTrue(all(len(row) <= 2 for row in keyboard.inline_keyboard))
                for b in buttons:
                    self.assertLessEqual(len(b.callback_data.encode()), 64)
                    action = menu.parse(b.callback_data)
                    self.assertIsNotNone(action)
                    if action[0] in {'p', 'r', 'g'}:
                        self.assertEqual(action[1], mode)
                seen.extend(menu.parse(b.callback_data)[2] for b in choices)
                self.assertEqual(any(b.text == '⬅️ Назад' for b in buttons), page > 0)
                self.assertEqual(any(b.text == 'Вперёд ➡️' for b in buttons), page + 1 < pages)
            self.assertEqual(seen, [s['id'] for s in scenarios.SCENARIOS])

    def test_new_registry_entries_appear_without_separate_menu_list(self):
        item = {**scenarios.SCENARIOS[0], 'id': 'new_case', 'title': 'Новая ситуация'}
        with patch.object(scenarios, 'SCENARIOS', [item]):
            text, keyboard = menu.catalog('dialog')
            self.assertIn('Новая ситуация', text)
            self.assertEqual(menu.parse(keyboard.inline_keyboard[0][0].callback_data),
                             ('r', 'dialog', 'new_case'))

    def test_malformed_stale_unknown_callbacks(self):
        for data in ['', 'tm:bad', 'tm:r:d:removed', 'tm:r:bad:secretary',
                     'tm:p:d:-1', 'tm:p:d:１', 'tm:p:d:1234567',
                     'tm:g:d:extra', 'tm:s:other', 'tm:s:0', 'tm:h:extra',
                     'tm:r:d:secretary:extra', 'tm:s:' + '1' * 100]:
            self.assertIsNone(menu.parse(data), data)
        pages = (len(scenarios.SCENARIOS) + 7) // 8
        self.assertIn(f'Страница {pages}/{pages}', menu.catalog('drill', 999)[0])

    def test_disabled_methodology_keeps_general_modes(self):
        for mode in menu.MODE_NAMES:
            _, keyboard = menu.catalog(mode, enabled=False)
            buttons = [b for row in keyboard.inline_keyboard for b in row]
            self.assertFalse(any(b.callback_data.startswith('tm:r:') for b in buttons))
            self.assertEqual(menu.parse(buttons[0].callback_data), ('g', mode, None))


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.telegram = Bot('123456:synthetic_test_token')
        self.actor = Actor(123, 1, 'manager', '101')
        self.pool = SimpleNamespace(fetchrow=AsyncMock(return_value={'timezone': 'UTC'}), execute=AsyncMock())
        self.session = {'id': 7, 'mode': 'dialog', 'client_id': 1, 'extension': '101',
                        'transcript': json.dumps([{'role': 'client', 'text': 'Алло, слушаю.'}])}
        self.patches = []
        self.answer = self.mock(Message, 'answer', AsyncMock())
        self.edit = self.mock(Message, 'edit_text', AsyncMock())
        self.ack = self.mock(CallbackQuery, 'answer', AsyncMock())
        self.mock(bot, 'PG_POOL', self.pool)
        self.resolve = self.mock(bot, 'resolve_actor', AsyncMock(return_value=self.actor))
        self.mock(bot, 'training_enabled', lambda: True)
        self.active = self.mock(bot.training_simulator, 'get_active_session', AsyncMock(return_value=None))
        self.start = self.mock(bot.training_simulator, 'start_session', AsyncMock(return_value=self.session))
        self.drill = self.mock(bot.training_simulator, 'start_drill_session', AsyncMock(return_value={**self.session, 'mode': 'drill'}))
        self.stop = self.mock(bot.training_simulator, 'end_session_early', AsyncMock(return_value=('Ответы сохранены, разбор поставлен в очередь.', None)))
        self.context = self.mock(bot.course_training, 'context', AsyncMock(return_value={'scenario': json.dumps(scenarios.SCENARIOS[0])}))
        self.audio_cost = self.mock(bot.course_training, 'record_audio_cost', AsyncMock())
        self.tts = self.mock(bot.tts, 'synthesize_ogg', AsyncMock(side_effect=bot.tts.TTSNotConfigured()))
        self.dialog_turn = self.mock(bot.training_simulator, 'handle_manager_turn', AsyncMock(return_value=bot.training_simulator.TurnResult('Ответ клиента', False)))
        self.drill_turn = self.mock(bot.training_simulator, 'handle_drill_turn', AsyncMock(return_value=bot.training_simulator.TurnResult('Возражение', False)))
        self.asr = self.mock(bot, 'dg_transcribe_bytes', AsyncMock(return_value=('[00:00 Спикер 0] Здравствуйте', 3)))
        self.get_file = self.mock(Bot, 'get_file', AsyncMock(return_value=SimpleNamespace(file_path='fake.ogg')))
        self.download = self.mock(Bot, 'download_file', AsyncMock(return_value=io.BytesIO(b'fake audio')))
        self.send_voice = self.mock(Bot, 'send_voice', AsyncMock())
        # Any accidental provider use fails the test before a network request.
        self.provider = self.mock(bot.training_simulator.claude.messages, 'create', AsyncMock(side_effect=AssertionError('Unexpected LLM request')))
        bot._ACTIONS_IN_PROGRESS.clear()

    def mock(self, target, name, value):
        p = patch.object(target, name, value)
        self.patches.append(p)
        return p.start()

    async def asyncTearDown(self):
        self.provider.assert_not_awaited()
        bot._ACTIONS_IN_PROGRESS.clear()
        for p in reversed(self.patches):
            p.stop()
        await self.telegram.session.close()

    def message(self, text=None, *, user_id=123, chat_id=None, chat_type='private', bot_author=False, voice=None):
        return Message(message_id=1, date=datetime.now(timezone.utc),
                       chat=Chat(id=chat_id or user_id, type=chat_type),
                       from_user=User(id=999 if bot_author else user_id, is_bot=bot_author, first_name='Synthetic'),
                       text=text, voice=voice).as_(self.telegram)

    def callback(self, data, *, user_id=123, chat_id=123, chat_type='private'):
        return CallbackQuery(id='test-callback', chat_instance='test-chat', data=data,
                             from_user=User(id=user_id, is_bot=False, first_name='Synthetic'),
                             message=self.message('Каталог', chat_id=chat_id, chat_type=chat_type, bot_author=True)).as_(self.telegram)

    async def test_start_help_and_all_six_reply_actions_dispatch_before_text(self):
        for text in ['/start', '/help', *menu.ACTIONS]:
            with self.subTest(text=text):
                await bot.router.propagate_event('message', self.message(text), bot=self.telegram)
        self.dialog_turn.assert_not_awaited()
        self.drill_turn.assert_not_awaited()
        self.asr.assert_not_awaited()
        self.stop.assert_not_awaited()
        self.assertTrue(any(isinstance(c.kwargs.get('reply_markup'), menu.ReplyKeyboardMarkup)
                            for c in self.answer.await_args_list))

    async def test_every_scenario_launches_correct_existing_function_in_both_modes(self):
        for code, mode in menu.MODES.items():
            for item in scenarios.SCENARIOS:
                await bot.menu_callback(self.callback(f"tm:r:{code}:{item['id']}"))
                start = self.start if mode == 'dialog' else self.drill
                self.assertEqual(start.await_args.args, (self.pool, self.actor, item['id'] if mode == 'dialog' else None, None, None))
        self.assertEqual(self.start.await_count, len(scenarios.SCENARIOS))
        self.assertEqual(self.drill.await_count, len(scenarios.SCENARIOS))
        self.assertEqual(self.ack.await_count, 2 * len(scenarios.SCENARIOS))
        self.assertIn('Режим: Короткая отработка', self.answer.await_args_list[-2].args[0])

    async def test_first_drill_voice_contains_only_objection_for_every_scenario(self):
        self.tts.side_effect = None
        self.tts.return_value = (b'fake-ogg', .01)
        for item in scenarios.SCENARIOS:
            pinned = {**item, 'exercises': scenarios.variants(item)}
            opening = training.exercise_opening(pinned['exercises'][0])
            self.context.return_value = {'scenario': json.dumps(pinned)}
            self.drill.return_value = {**self.session, 'mode': 'drill',
                                      'transcript': json.dumps([{'role': 'client', 'text': opening}])}
            await bot.menu_callback(self.callback(f"tm:r:o:{item['id']}"))
            self.tts.assert_awaited_with(item['objection'])
            self.assertEqual(self.answer.await_args.args[0], opening)
            self.assertIn('Упражнение 1:', opening)
        self.assertEqual(self.send_voice.await_count, len(scenarios.SCENARIOS))
        self.assertEqual(self.audio_cost.await_count, len(scenarios.SCENARIOS))

    async def test_next_drill_voice_skips_exercise_context_and_coaching(self):
        item = scenarios.variants(scenarios.SCENARIOS[3])[1]
        opening = training.exercise_opening(item)
        self.tts.side_effect = None
        self.tts.return_value = (b'fake-ogg', .01)
        self.drill_turn.return_value = bot.training_simulator.TurnResult(
            opening, False, note='Ответ сохранён.', speech_text=item['objection'])
        await bot._process_turn(self.message('Мой ответ'), {**self.session, 'mode': 'drill'}, 'Мой ответ')
        self.tts.assert_awaited_once_with(item['objection'])
        self.assertEqual([c.args[0] for c in self.answer.await_args_list], ['Ответ сохранён.', opening])

    async def test_drill_context_fallback_is_not_duplicated(self):
        item = scenarios.variants(scenarios.SCENARIOS[3])[0]
        opening = training.exercise_opening(item)
        result = bot.training_simulator.TurnResult(opening, False, speech_text=item['objection'])
        for error in (bot.tts.TTSNotConfigured(), bot.tts.TTSError('synthetic failure')):
            self.answer.reset_mock()
            self.audio_cost.reset_mock()
            self.tts.side_effect = error
            if isinstance(error, bot.tts.TTSError):
                with self.assertLogs('callbot-trainer', level='ERROR'):
                    cost = await bot._send_training_reply(self.message(), result, 7, mode='drill')
            else:
                cost = await bot._send_training_reply(self.message(), result, 7, mode='drill')
            self.assertEqual(cost, 0)
            self.answer.assert_awaited_once_with(opening, reply_markup=bot._stop_keyboard(7))
            self.audio_cost.assert_not_awaited()

    async def test_drill_summary_is_text_only(self):
        result = bot.training_simulator.TurnResult('Отработка закончена: зачтено 3 из 5.', True)
        await bot._send_training_reply(self.message(), result, 7, mode='drill')
        self.tts.assert_not_awaited()
        self.answer.assert_awaited_once_with(result.reply_text, reply_markup=None)

    async def test_full_dialogue_voice_preserves_entire_client_reply(self):
        self.tts.side_effect = None
        self.tts.return_value = (b'fake-ogg', .01)
        result = bot.training_simulator.TurnResult('Расскажите, чем ваше предложение нам поможет.', False)
        await bot._send_training_reply(self.message(), result, 7, mode='dialog')
        self.tts.assert_awaited_once_with(result.reply_text)
        self.answer.assert_not_awaited()

    async def test_legacy_drill_still_speaks_plain_objection(self):
        self.context.return_value = None
        self.tts.side_effect = None
        self.tts.return_value = (b'fake-ogg', .01)
        self.drill.return_value = {**self.session, 'mode': 'drill',
                                  'transcript': json.dumps([{'role': 'client', 'text': 'Это дорого для нас.'}])}
        await bot._launch(self.message(), self.actor, None, None, mode='drill')
        self.tts.assert_awaited_once_with('Это дорого для нас.')

    async def test_pagination_edits_same_message_preserves_mode_and_home(self):
        for data in ['tm:p:d:1', 'tm:p:d:2', 'tm:p:d:1', 'tm:p:d:0']:
            await bot.menu_callback(self.callback(data))
            self.assertIn('Разговор целиком', self.edit.await_args.args[0])
        self.assertEqual(self.edit.await_count, 4)
        self.assertEqual(self.answer.await_count, 0)
        await bot.menu_callback(self.callback('tm:h'))
        self.assertIsInstance(self.answer.await_args.kwargs['reply_markup'], menu.ReplyKeyboardMarkup)
        self.start.assert_not_awaited()
        self.drill.assert_not_awaited()

    async def test_short_button_launches_immediately_without_catalog(self):
        await bot.router.propagate_event('message', self.message(menu.DRILL), bot=self.telegram)
        self.drill.assert_awaited_once_with(self.pool, self.actor, None, None, None)
        self.assertFalse(any(isinstance(c.kwargs.get('reply_markup'), menu.InlineKeyboardMarkup)
                             and any(b.callback_data.startswith('tm:r:') for row in c.kwargs['reply_markup'].inline_keyboard for b in row)
                             for c in self.answer.await_args_list))
        self.provider.assert_not_awaited()

    async def test_old_short_catalog_navigation_no_longer_offers_scenarios(self):
        await bot.menu_callback(self.callback('tm:p:o:1'))
        self.edit.assert_not_awaited()
        self.drill.assert_not_awaited()
        self.assertIn('без выбора ситуации', self.answer.await_args.args[0])

    async def test_repeat_preserves_short_mode(self):
        self.pool.fetchrow.return_value = {'mode': 'drill'}
        await bot.repeat_command(self.message('/repeat'))
        self.drill.assert_awaited_once_with(self.pool, self.actor, 'repeat', None, None)
        self.start.assert_not_awaited()

    async def test_existing_session_blocks_start_catalog_repeat_and_new_scenario(self):
        self.active.return_value = self.session
        for text in [menu.START, menu.SCENARIOS, menu.DRILL, menu.REPEAT, '/train secretary']:
            await bot.router.propagate_event('message', self.message(text), bot=self.telegram)
        await bot.menu_callback(self.callback('tm:r:d:support'))
        self.start.assert_not_awaited()
        self.drill.assert_not_awaited()
        self.stop.assert_not_awaited()
        self.assertIn('Тренировка уже идёт', self.answer.await_args.args[0])
        await bot.menu_callback(self.callback('tm:c'))
        self.assertIn('Продолжайте отвечать', self.answer.await_args.args[0])

    async def test_db_refusal_does_not_bypass_launch(self):
        self.start.side_effect = training.TrainingLimit('Обработка приостановлена.')
        await bot.menu_callback(self.callback('tm:r:d:secretary'))
        self.assertIn('приостановлена', self.answer.await_args.args[0])

    async def test_fast_double_click_creates_only_one_session_including_legacy(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait_start(*args):
            entered.set()
            await release.wait()
            return self.session
        self.start.side_effect = wait_start
        self.mock(bot, 'training_enabled', lambda: False)
        first = asyncio.create_task(bot.train_mode_callback(self.callback('train_mode:dialog')))
        await entered.wait()
        await bot.train_mode_callback(self.callback('train_mode:dialog'))
        self.assertIn('ещё выполняется', self.answer.await_args.args[0])
        release.set()
        await first
        self.start.assert_awaited_once()
        self.assertFalse(bot._ACTIONS_IN_PROGRESS)

    async def test_repeat_calls_existing_logic_and_explains_missing_history(self):
        self.start.side_effect = training.TrainingLimit('Завершённой тренировки по курсу пока нет.')
        await bot.repeat_command(self.message('/repeat'))
        self.assertEqual(self.start.await_args.args[2], 'repeat')
        self.assertIn('пока нет', self.answer.await_args.args[0])
        self.assertIsInstance(self.answer.await_args.kwargs['reply_markup'], menu.ReplyKeyboardMarkup)

    async def test_stop_no_session_does_not_analyze_and_stale_stop_preserves_current(self):
        await bot.stop_command(self.message('/stop'))
        self.stop.assert_not_awaited()
        self.active.return_value = self.session
        await bot.menu_callback(self.callback('tm:s:6'))
        self.stop.assert_not_awaited()
        await bot.menu_callback(self.callback('tm:s:7'))
        self.stop.assert_awaited_once_with(self.pool, self.session)
        self.assertIn('сохранены', self.answer.await_args.args[0])
        self.assertIsInstance(self.answer.await_args.kwargs['reply_markup'], menu.ReplyKeyboardMarkup)

    async def test_stale_unknown_disabled_callback_never_launches(self):
        for data in ['tm:r:d:removed', 'tm:r:invalid:secretary', 'tm:p:d:-1']:
            await bot.menu_callback(self.callback(data))
        self.mock(bot, 'training_enabled', lambda: False)
        await bot.menu_callback(self.callback('tm:r:d:secretary'))
        self.start.assert_not_awaited()
        self.drill.assert_not_awaited()
        self.assertEqual(self.ack.await_count, 4)

    async def test_legacy_stop_button_cannot_stop_a_newer_session(self):
        self.active.return_value = {**self.session, 'started_at': datetime.now(timezone.utc) + timedelta(minutes=1)}
        await bot.train_stop_callback(self.callback('train_stop'))
        self.stop.assert_not_awaited()
        self.assertIn('другой тренировке', self.answer.await_args.args[0])

    async def test_foreign_private_group_and_unauthorized_callbacks_are_acknowledged(self):
        for cq in [self.callback('tm:s:7', user_id=456),
                   self.callback('tm:r:d:secretary', chat_id=-123, chat_type='group')]:
            await bot.menu_callback(cq)
        self.resolve.assert_not_awaited()
        self.resolve.return_value = None
        await bot.menu_callback(self.callback('tm:r:d:secretary'))
        self.resolve.assert_awaited_once_with(self.pool, 123)
        self.assertEqual(self.ack.await_count, 3)
        self.start.assert_not_awaited()
        self.stop.assert_not_awaited()

    async def test_callbacks_resolve_clicker_and_all_old_commands_work(self):
        for text in ['/train secretary', '/train возражения expensive', '/train dialog', '/train drill', '/train', '/scenarios']:
            await bot.router.propagate_event('message', self.message(text), bot=self.telegram)
        self.assertEqual(self.start.await_count, 2)
        self.assertEqual(self.drill.await_count, 2)
        self.assertEqual(self.drill.await_args_list[0].args[2], 'expensive')
        await bot.train_mode_callback(self.callback('train_mode:drill'))
        self.resolve.assert_awaited_with(self.pool, 123)
        self.active.return_value = self.session
        await bot.train_stop_callback(self.callback('train_stop'))
        self.stop.assert_awaited_once_with(self.pool, self.session)

    async def test_menu_actions_during_session_are_never_manager_turns(self):
        self.active.return_value = self.session
        for action in menu.ACTIONS:
            await bot.router.propagate_event('message', self.message(action), bot=self.telegram)
        self.dialog_turn.assert_not_awaited()
        self.drill_turn.assert_not_awaited()
        self.asr.assert_not_awaited()
        self.provider.assert_not_awaited()

    async def test_voice_and_text_keep_existing_turn_handlers_and_completion_menu(self):
        self.active.return_value = self.session
        await bot.router.propagate_event('message', self.message('Добрый день'), bot=self.telegram)
        self.dialog_turn.assert_awaited_with(self.pool, self.session, 'Добрый день')
        voice = Voice(file_id='fake-id', file_unique_id='fake-unique', duration=3)
        await bot.router.propagate_event('message', self.message(voice=voice), bot=self.telegram)
        self.dialog_turn.assert_awaited_with(self.pool, self.session, 'Здравствуйте')
        self.audio_cost.assert_awaited_once()
        self.active.return_value = {**self.session, 'mode': 'drill'}
        self.drill_turn.return_value = bot.training_simulator.TurnResult('', True)
        await bot.router.propagate_event('message', self.message('Проверим условия'), bot=self.telegram)
        self.drill_turn.assert_awaited_once()
        self.assertIsInstance(self.answer.await_args.kwargs['reply_markup'], menu.ReplyKeyboardMarkup)

    async def test_no_active_voice_text_do_not_call_external_services(self):
        await bot.voice_turn(self.message(voice=Voice(file_id='fake-id', file_unique_id='fake', duration=3)))
        await bot.text_turn(self.message('Здравствуйте'))
        self.asr.assert_not_awaited()
        self.dialog_turn.assert_not_awaited()

    async def test_assignment_deep_link_keeps_client_extension_checks(self):
        assignment = {'extension': '101', 'client_id': 1, 'topic': 'secretary',
                      'assigned_by_telegram_user_id': 456, 'mode': 'drill'}
        self.pool.fetchrow.side_effect = [assignment]
        await bot.start_command(self.message('/start train_5'), CommandObject(command='start', args='train_5'))
        self.drill.assert_awaited_once_with(self.pool, self.actor, 'secretary', '456', 5)
        self.pool.execute.assert_not_awaited()  # Course DB transaction owns assignment consumption.
        self.pool.fetchrow.side_effect = None
        self.pool.fetchrow.return_value = {**assignment, 'client_id': 2}
        await bot.start_command(self.message('/start train_6'), CommandObject(command='start', args='train_6'))
        self.assertEqual(self.drill.await_count, 1)

    async def test_callback_error_acknowledges_and_releases_launch_guard(self):
        self.start.side_effect = RuntimeError('synthetic failure')
        with self.assertLogs('callbot-trainer', level='ERROR'):
            await bot.menu_callback(self.callback('tm:r:d:secretary'))
        self.ack.assert_awaited_once()
        self.assertFalse(bot._ACTIONS_IN_PROGRESS)
        self.assertIn('Не удалось', self.answer.await_args.args[0])
