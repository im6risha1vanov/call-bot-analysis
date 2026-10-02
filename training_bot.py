"""
Отдельный Telegram-бот тренажёра возражений (Этап 4).

Почему отдельный бот, а не команда в основном: голосовое в основном боте
раньше означало загрузку записи звонка, а в тренажёре — реплику клиента.
Два смысла в одном чате ломали тренировку. Основной бот записи больше не
принимает (звонки из Mango); этот чат — только клиент в ролевой игре.

Сама логика тренировки живёт в training_simulator.py и от Telegram не
зависит; здесь только слой бота.

Личность менеджера определяется по telegram_user_id — он в Telegram общий для
всех ботов, поэтому повторно привязывать добавочный через /invite не нужно.
Но написать боту первым менеджер обязан (правило Telegram) — для назначенных
РОПом тренировок это решает диплинк из основного бота.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / '.env')
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('callbot-trainer')

import tts  # noqa: E402  — читает переменные окружения при импорте, только после load_dotenv
import training_simulator  # noqa: E402
import training_menu as menu
from methodology import training as course_training
from methodology.runtime import training_enabled
from deepgram_client import transcribe_bytes as dg_transcribe_bytes  # noqa: E402
from tools import resolve_actor  # noqa: E402

BOT_TOKEN = os.environ['TRAIN_BOT_TOKEN']

router = Router()
PG_POOL: asyncpg.Pool | None = None
# An action remains busy through the first reply, including legacy mode's paid
# opening. Course sessions additionally retain their transaction-scoped DB lock.
_ACTIONS_IN_PROGRESS: set[tuple[int, str]] = set()

START_TEXT = (
    'Это тренажёр возражений. Я играю клиента, которому вы звоните вхолодную — '
    'занятого и не настроенного разговаривать.\n\n'
    'Команда /train начинает тренировку. Отвечать можно голосовыми сообщениями '
    '(как в реальном звонке) или текстом. Закончить в любой момент — кнопкой '
    'под репликой клиента или командой /stop.\n\n'
    'Разбор реальных звонков и вопросы по статистике — в основном боте, здесь только тренировка.'
)


def start_text():
    if training_enabled():
        greeting = 'Тренажёр продаж и сопровождения.'
    else:
        greeting = 'Тренажёр возражений: я играю клиента холодного звонка.'
    return (greeting + '\n\n'
            '🎯 Начать тренировку / 📋 Ситуации — выбрать ситуацию для полного разговора.\n'
            '⚡ Короткая отработка — пять упражнений в выбранной ситуации.\n'
            '🔁 Повторить — завершённая ситуация с изменёнными обстоятельствами.\n'
            '⏹ Завершить — сохранить ответы и закончить тренировку.\n'
            '❓ Помощь — показать эту подсказку.\n\n'
            'Отвечайте голосовыми сообщениями или текстом. После завершения получите разбор. '
            'Тренироваться нужно в личном чате с ботом.\n'
            'Команды также работают: /train, /scenarios, /repeat, /stop, /help.')


def _stop_keyboard(session_id: int | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Завершить тренировку', callback_data=f'tm:s:{session_id}' if session_id else 'train_stop')
    ]])


async def _send_training_reply(message: Message, result: training_simulator.TurnResult, session_id: int | None = None) -> float:
    """Голос — основной путь; текст — откат, если TTS ещё не настроен
    (см. tts.py) или синтез не удался.

    result.note (разбор предыдущего ответа в режиме отработки) уходит текстом
    отдельно: это голос тренера, а не клиента, озвучивать его нельзя."""
    # Реплики тренажёра — обычная речь, разметка тут не нужна: шлём как есть,
    # без экранирования и без parse_mode. С экранированием, но без parse_mode
    # в чат уезжали бы &quot; вместо кавычек.
    if result.note:
        await message.answer(result.note)
    if not result.reply_text:
        return 0.0
    # Кнопка висит на реплике клиента, пока тренировка идёт: выйти можно в любой
    # момент, а не только досидев до конца.
    markup = None if result.ended else _stop_keyboard(session_id)
    audio_cost = 0.0
    try:
        ogg_bytes, _cost = await tts.synthesize_ogg(result.reply_text)
        audio_cost = _cost
        if session_id is not None:
            await course_training.record_audio_cost(PG_POOL, session_id, audio_cost)
        await message.bot.send_voice(message.chat.id, voice=BufferedInputFile(ogg_bytes, filename='client.ogg'),
                                      reply_markup=markup)
    except tts.TTSNotConfigured:
        await message.answer(result.reply_text, reply_markup=markup)
    except Exception:
        log.exception('ошибка синтеза речи тренажёра')
        await message.answer(result.reply_text, reply_markup=markup)
    return audio_cost


async def _process_turn(message: Message, session, manager_text: str) -> None:
    handler = (training_simulator.handle_drill_turn if session['mode'] == 'drill'
               else training_simulator.handle_manager_turn)
    try:
        result = await handler(PG_POOL, session, manager_text)
    except Exception:
        log.exception('ошибка хода тренажёра, session id=%s', session['id'])
        await message.answer('Не удалось обработать ответ, попробуйте ещё раз.')
        return
    await _send_training_reply(message, result, session['id'])
    if result.ended:
        level_line = f' Уровень: {result.level}.' if result.level else ''
        await message.answer(f'Тренировка завершена.{level_line} Подробности доступны агенту РОПа.',
                             reply_markup=menu.main_keyboard())


INTRO = {
    'dialog': ('Тренировка началась: обычный холодный звонок, клиент не настроен разговаривать. '
               'Отвечайте голосовыми сообщениями (или текстом).'),
    'drill': (f'Отработка возражений: {training_simulator.DRILL_SIZE} штук подряд. На каждое отвечайте так, '
              'как ответили бы в реальном звонке — голосовым или текстом. После каждого ответа '
              'скажу, зачтено или нет.'),
}


async def _launch(message: Message, actor, topic: str | None, assigned_by: str | None,
                   mode: str = 'dialog', assignment_id: int | None = None) -> int | None:
    key = (actor.client_id, training_simulator.actor_key(actor))
    if key in _ACTIONS_IN_PROGRESS:
        await message.answer('Предыдущее действие ещё выполняется. Дождитесь ответа бота.')
        return None
    _ACTIONS_IN_PROGRESS.add(key)
    try:
        return await _launch_session(message, actor, topic, assigned_by, mode, assignment_id)
    finally:
        _ACTIONS_IN_PROGRESS.discard(key)


async def _busy(message: Message, session):
    await message.answer('Тренировка уже идёт. Продолжайте отвечать голосом или текстом '
                         'либо завершите её перед выбором новой ситуации.',
                         reply_markup=menu.active_keyboard(session['id']))


async def _launch_session(message: Message, actor, topic, assigned_by, mode, assignment_id):
    existing = await training_simulator.get_active_session(PG_POOL, actor)
    if existing is not None:
        await _busy(message, existing)
        return
    try:
        if mode == 'drill':
            session = await training_simulator.start_drill_session(PG_POOL, actor, topic, assigned_by, assignment_id)
        else:
            session = await training_simulator.start_session(PG_POOL, actor, topic, assigned_by, assignment_id)
    except course_training.TrainingLimit as exc:
        await message.answer(str(exc), reply_markup=menu.main_keyboard())
        return None
    opening = json.loads(session['transcript'])[0]['text']
    context = await course_training.context(PG_POOL, session['id'])
    if assignment_id is not None and not context:
        await PG_POOL.execute('UPDATE pending_train_assignments SET consumed=true WHERE id=$1 AND client_id=$2 AND extension=$3',
                              assignment_id, actor.client_id, actor.extension)
    introduction = course_training.intro(json.loads(context['scenario']), mode) if context else INTRO[mode]
    await message.answer(f'Режим: {menu.MODE_NAMES[mode]}.\n{introduction}\n'
                         'Завершить можно кнопкой «⏹ Завершить».', reply_markup=menu.main_keyboard())
    await _send_training_reply(message, training_simulator.TurnResult(opening, False), session['id'])
    return session['id']


async def _require_employee(message: Message):
    """Руководителя тоже пускаем: он должен иметь возможность пройти тренажёр
    сам и решить, годится ли инструмент для отдела. Его сессии помечаются
    пробными и в статистику отдела не попадают."""
    if message.chat.type != 'private' or message.chat.id != message.from_user.id:
        await message.answer('Тренировки доступны в личном чате с ботом. Откройте его и нажмите /start.')
        return None
    if PG_POOL is None:
        return None
    actor = await resolve_actor(PG_POOL, message.from_user.id)
    if actor is None:
        await message.answer('Вы не привязаны к системе. Попросите руководителя подключить вас к основному боту.')
        return None
    return actor


@router.message(CommandStart())
async def start_command(message: Message, command: CommandObject):
    """Диплинк вида ?start=train_<id> приходит сюда из основного бота, когда
    РОП назначает тренировку: одно нажатие и запускает бота (Telegram иначе не
    даст ему написать первым), и стартует назначенную сессию."""
    payload = (command.args or '').strip()
    if not payload.startswith('train_'):
        await help_command(message)
        return

    actor = await _require_employee(message)
    if actor is None:
        return
    try:
        assignment_id = int(payload.removeprefix('train_'))
    except ValueError:
        await help_command(message)
        return

    assignment = await PG_POOL.fetchrow(
        'SELECT * FROM pending_train_assignments WHERE id=$1 AND consumed=false', assignment_id
    )
    if not assignment or assignment['extension'] != actor.extension or assignment['client_id'] != actor.client_id:
        await message.answer('Это назначение не для вас или уже использовано. Выберите «🎯 Начать тренировку».',
                             reply_markup=menu.main_keyboard())
        return
    await _launch(message, actor, topic=assignment['topic'],
                  assigned_by=str(assignment['assigned_by_telegram_user_id']),
                  mode=assignment['mode'], assignment_id=assignment_id)


@router.callback_query(F.data == 'train_stop')
async def train_stop_callback(cq: CallbackQuery):
    await _handle_callback(cq, ('s', None, None))


@router.message(Command('stop'))
async def stop_command(message: Message):
    """То же, что кнопка — на случай, если она уехала вверх по переписке."""
    actor = await _require_employee(message)
    if actor is None:
        return
    await _stop(message, actor)


async def _stop(message: Message, actor, session_id: int | None = None, *, legacy_button=False):
    key = (actor.client_id, training_simulator.actor_key(actor))
    if key in _ACTIONS_IN_PROGRESS:
        await message.answer('Предыдущее действие ещё выполняется. Дождитесь ответа бота.')
        return
    _ACTIONS_IN_PROGRESS.add(key)
    try:
        session = await training_simulator.get_active_session(PG_POOL, actor)
        if session is None:
            await message.answer('Активной тренировки нет. Выберите «🎯 Начать тренировку».',
                                 reply_markup=menu.main_keyboard())
            return
        older_button = (legacy_button and session.get('started_at') is not None
                        and int(message.date.timestamp()) < int(session['started_at'].timestamp()))
        if older_button or (session_id is not None and session['id'] != session_id):
            await message.answer('Эта кнопка относится к другой тренировке. Текущая тренировка продолжается.',
                                 reply_markup=menu.active_keyboard(session['id']))
            return
        text, level = await training_simulator.end_session_early(PG_POOL, session)
        level_line = f' Уровень: {level}.' if level else ''
        await message.answer(f'{text}{level_line}', reply_markup=menu.main_keyboard())
    except Exception:
        log.exception('ошибка досрочного завершения')
        await message.answer('Не удалось завершить тренировку, попробуйте ещё раз.')
    finally:
        _ACTIONS_IN_PROGRESS.discard(key)


@router.message(Command('help'))
async def help_command(message: Message):
    if message.chat.type != 'private':
        await message.answer('Откройте личный чат с ботом и нажмите /start — там доступно меню тренировок.')
        return
    await message.answer(start_text(), reply_markup=menu.main_keyboard())


def _mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Разговор целиком', callback_data='train_mode:dialog')],
        [InlineKeyboardButton(text='Отработка возражений', callback_data='train_mode:drill')],
    ])


@router.message(Command('train'))
async def train_command(message: Message):
    """Режим можно задать сразу (/train возражения), иначе спрашиваем кнопками —
    менеджеру не нужно помнить синтаксис."""
    actor = await _require_employee(message)
    if actor is None:
        return
    arg = (message.text or '').partition(' ')[2].strip().lower()
    if training_enabled() and arg and arg.split(' ', 1)[0] in ('drill', 'возражения', 'отработка'):
        await _launch(message, actor, topic=arg.partition(' ')[2] or None, assigned_by=None, mode='drill')
    elif training_enabled() and course_training.find(arg):
        await _launch(message, actor, topic=arg, assigned_by=None, mode='dialog')
    elif arg in ('drill', 'возражения', 'отработка'):
        await _launch(message, actor, topic=None, assigned_by=None, mode='drill')
    elif arg in ('dialog', 'разговор', 'звонок'):
        await _launch(message, actor, topic=None, assigned_by=None, mode='dialog')
    else:
        await message.answer('Что тренируем?', reply_markup=_mode_keyboard())


@router.message(Command('scenarios'))
async def scenarios_command(message: Message):
    await _choose(message, 'dialog')


async def _choose(message: Message, mode: str):
    actor = await _require_employee(message)
    if actor is None:
        return
    await _catalog(message, actor, mode)


async def _catalog(message: Message, actor, mode: str, page=0, *, edit=False):
    session = await training_simulator.get_active_session(PG_POOL, actor)
    if session:
        await _busy(message, session)
        return
    text, markup = menu.catalog(mode, page, enabled=training_enabled())
    if edit:
        from aiogram.exceptions import TelegramBadRequest
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if 'message is not modified' not in str(exc).lower():
                raise
    else:
        await message.answer(text, reply_markup=markup)


@router.message(Command('repeat'))
async def repeat_command(message: Message):
    actor = await _require_employee(message)
    if actor is None:
        return
    if not training_enabled():
        await message.answer('Повтор сценария по курсу сейчас выключен.')
        return
    await _launch(message, actor, topic='repeat', assigned_by=None)


@router.callback_query(F.data.startswith('train_mode:'))
async def train_mode_callback(cq: CallbackQuery):
    mode = cq.data.rsplit(':', 1)[1]
    await _handle_callback(cq, ('g', mode, None) if mode in menu.MODE_NAMES else None)


@router.callback_query(F.data.startswith('tm:'))
async def menu_callback(cq: CallbackQuery):
    await _handle_callback(cq, menu.parse(cq.data))


async def _handle_callback(cq: CallbackQuery, action):
    # Always acknowledge, including malformed/stale callbacks and provider errors.
    if not isinstance(cq.message, Message):
        await cq.answer('Сообщение недоступно. Откройте личный чат с ботом и нажмите /start.', show_alert=True)
        return
    if cq.message.chat.type != 'private' or cq.message.chat.id != cq.from_user.id:
        await cq.answer('Эти кнопки доступны только в вашем личном чате с ботом.', show_alert=True)
        return
    await cq.answer()
    try:
        if PG_POOL is None:
            await cq.message.answer('Бот запускается. Попробуйте чуть позже.')
            return
        actor = await resolve_actor(PG_POOL, cq.from_user.id)
        if actor is None:
            await cq.message.answer('Вы не привязаны к системе. Попросите руководителя подключить вас к основному боту.')
            return
        if action is None:
            await cq.message.answer('Эта кнопка устарела или неизвестна. Откройте «📋 Ситуации».',
                                    reply_markup=menu.main_keyboard())
            return
        kind, mode, value = action
        if kind == 'h':
            await cq.message.edit_text('Выберите действие в меню под полем ввода.', reply_markup=None)
            await help_command(cq.message)
        elif kind == 'p':
            await _catalog(cq.message, actor, mode, value, edit=True)
        elif kind == 's':
            await _stop(cq.message, actor, value, legacy_button=value is None)
        elif kind == 'c':
            session = await training_simulator.get_active_session(PG_POOL, actor)
            if session:
                await cq.message.answer('Продолжайте отвечать на последнюю реплику клиента голосом или текстом.',
                                        reply_markup=menu.main_keyboard())
            else:
                await cq.message.answer('Активной тренировки нет. Выберите «🎯 Начать тренировку».',
                                        reply_markup=menu.main_keyboard())
        elif kind in {'g', 'r'}:
            if kind == 'r' and not training_enabled():
                await cq.message.answer('Сценарии по курсу сейчас выключены. Откройте «📋 Ситуации».',
                                        reply_markup=menu.main_keyboard())
                return
            await _launch(cq.message, actor, topic=value, assigned_by=None, mode=mode)
    except Exception:
        log.exception('ошибка кнопки тренажёра')
        await cq.message.answer('Не удалось выполнить действие. Попробуйте ещё раз или откройте меню через /help.')


@router.message(F.text.in_(menu.ACTIONS))
async def menu_action(message: Message):
    if message.text in {menu.START, menu.SCENARIOS}:
        await _choose(message, 'dialog')
    elif message.text == menu.DRILL:
        await _choose(message, 'drill')
    elif message.text == menu.REPEAT:
        await repeat_command(message)
    elif message.text == menu.STOP:
        await stop_command(message)
    elif message.text == menu.HELP:
        await help_command(message)


@router.message(F.voice)
async def voice_turn(message: Message):
    """Распознаём тем же Deepgram, что и реальные звонки — тренировка и работа
    проходят через один движок, а не через премиум-расшифровку Telegram,
    которой у ботов всё равно нет."""
    actor = await _require_employee(message)
    if actor is None:
        return
    session = await training_simulator.get_active_session(PG_POOL, actor)
    if session is None:
        await message.answer('Нет активной тренировки. Выберите «🎯 Начать тренировку».', reply_markup=menu.main_keyboard())
        return
    try:
        file = await message.bot.get_file(message.voice.file_id)
        buf = await message.bot.download_file(file.file_path)
        raw_transcript, _dur = await dg_transcribe_bytes(buf.read())
        await course_training.record_audio_cost(PG_POOL, session['id'], _dur / 60 * training_simulator.DG_PRICE_PER_MIN_USD)
        manager_text = training_simulator.strip_speaker_tags(raw_transcript) or '(не удалось распознать речь)'
    except Exception:
        log.exception('ошибка распознавания голосового сообщения, session id=%s', session['id'])
        await message.answer('Не удалось распознать голосовое сообщение, попробуйте ещё раз.')
        return
    await _process_turn(message, session, manager_text)


@router.message(F.text)
async def text_turn(message: Message):
    actor = await _require_employee(message)
    if actor is None:
        return
    session = await training_simulator.get_active_session(PG_POOL, actor)
    if session is None:
        await message.answer('Нет активной тренировки. Выберите «🎯 Начать тренировку».', reply_markup=menu.main_keyboard())
        return
    await _process_turn(message, session, message.text or '')


async def main() -> None:
    global PG_POOL
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    PG_POOL = await asyncpg.create_pool(os.environ['DATABASE_URL'], min_size=1, max_size=3)
    log.info('training bot started')
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await PG_POOL.close()


if __name__ == '__main__':
    asyncio.run(main())
