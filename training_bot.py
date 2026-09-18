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
import html
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
from deepgram_client import transcribe_bytes as dg_transcribe_bytes  # noqa: E402
from tools import resolve_actor  # noqa: E402

BOT_TOKEN = os.environ['TRAIN_BOT_TOKEN']

router = Router()
PG_POOL: asyncpg.Pool | None = None

START_TEXT = (
    'Это тренажёр возражений. Я играю клиента, которому вы звоните вхолодную — '
    'занятого и не настроенного разговаривать.\n\n'
    'Команда /train начинает тренировку. Отвечать можно голосовыми сообщениями '
    '(как в реальном звонке) или текстом. Закончить в любой момент — кнопкой '
    'под репликой клиента или командой /stop.\n\n'
    'Разбор реальных звонков и вопросы по статистике — в основном боте, здесь только тренировка.'
)


def _stop_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Завершить тренировку', callback_data='train_stop')
    ]])


async def _send_training_reply(message: Message, result: training_simulator.TurnResult) -> None:
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
        return
    # Кнопка висит на реплике клиента, пока тренировка идёт: выйти можно в любой
    # момент, а не только досидев до конца.
    markup = None if result.ended else _stop_keyboard()
    try:
        ogg_bytes, _cost = await tts.synthesize_ogg(result.reply_text)
        await message.bot.send_voice(message.chat.id, voice=BufferedInputFile(ogg_bytes, filename='client.ogg'),
                                      reply_markup=markup)
    except tts.TTSNotConfigured:
        await message.answer(result.reply_text, reply_markup=markup)
    except Exception:
        log.exception('ошибка синтеза речи тренажёра')
        await message.answer(result.reply_text, reply_markup=markup)


async def _process_turn(message: Message, session, manager_text: str) -> None:
    handler = (training_simulator.handle_drill_turn if session['mode'] == 'drill'
               else training_simulator.handle_manager_turn)
    try:
        result = await handler(PG_POOL, session, manager_text)
    except Exception:
        log.exception('ошибка хода тренажёра, session id=%s', session['id'])
        await message.answer('Не удалось обработать ответ, попробуйте ещё раз.')
        return
    await _send_training_reply(message, result)
    if result.ended:
        level_line = f' Уровень: {result.level}.' if result.level else ''
        await message.answer(f'Тренировка завершена.{level_line} Подробности доступны агенту РОПа.')


INTRO = {
    'dialog': ('Тренировка началась: обычный холодный звонок, клиент не настроен разговаривать. '
               'Отвечайте голосовыми сообщениями (или текстом).'),
    'drill': (f'Отработка возражений: {training_simulator.DRILL_SIZE} штук подряд. На каждое отвечайте так, '
              'как ответили бы в реальном звонке — голосовым или текстом. После каждого ответа '
              'скажу, зачтено или нет.'),
}


async def _launch(message: Message, actor, topic: str | None, assigned_by: str | None,
                   mode: str = 'dialog') -> None:
    existing = await training_simulator.get_active_session(PG_POOL, actor)
    if existing is not None:
        await message.answer('У вас уже есть незавершённая тренировка — закончите её, прежде чем начинать новую.')
        return
    client = await PG_POOL.fetchrow('SELECT timezone FROM clients WHERE id=$1', actor.client_id)
    tz_name = client['timezone'] if client else 'Europe/Moscow'
    done_today = await training_simulator.sessions_today(PG_POOL, actor.client_id, training_simulator.actor_key(actor), tz_name)
    if done_today >= training_simulator.MAX_SESSIONS_PER_DAY:
        await message.answer(
            f'Уже {done_today} тренировки сегодня — дневной лимит ({training_simulator.MAX_SESSIONS_PER_DAY}) исчерпан.')
        return
    if mode == 'drill':
        session = await training_simulator.start_drill_session(PG_POOL, actor, topic, assigned_by)
    else:
        session = await training_simulator.start_session(PG_POOL, actor, topic, assigned_by)
    opening = json.loads(session['transcript'])[0]['text']
    await message.answer(INTRO[mode])
    await _send_training_reply(message, training_simulator.TurnResult(opening, False))


async def _require_employee(message: Message):
    """Руководителя тоже пускаем: он должен иметь возможность пройти тренажёр
    сам и решить, годится ли инструмент для отдела. Его сессии помечаются
    пробными и в статистику отдела не попадают."""
    if PG_POOL is None:
        return None
    actor = await resolve_actor(PG_POOL, message.from_user.id)
    if actor is None:
        await message.answer('Вы не привязаны к системе. Попросите РОПа прислать ссылку /invite в основном боте.')
        return None
    return actor


@router.message(CommandStart())
async def start_command(message: Message, command: CommandObject):
    """Диплинк вида ?start=train_<id> приходит сюда из основного бота, когда
    РОП назначает тренировку: одно нажатие и запускает бота (Telegram иначе не
    даст ему написать первым), и стартует назначенную сессию."""
    payload = (command.args or '').strip()
    if not payload.startswith('train_'):
        await message.answer(START_TEXT)
        return

    actor = await _require_employee(message)
    if actor is None:
        return
    try:
        assignment_id = int(payload.removeprefix('train_'))
    except ValueError:
        await message.answer(START_TEXT)
        return

    assignment = await PG_POOL.fetchrow(
        'SELECT * FROM pending_train_assignments WHERE id=$1 AND consumed=false', assignment_id
    )
    if not assignment or assignment['extension'] != actor.extension or assignment['client_id'] != actor.client_id:
        await message.answer('Это назначение не для вас или уже использовано. Начать обычную тренировку: /train')
        return
    await PG_POOL.execute('UPDATE pending_train_assignments SET consumed=true WHERE id=$1', assignment_id)
    await _launch(message, actor, topic=assignment['topic'],
                  assigned_by=str(assignment['assigned_by_telegram_user_id']),
                  mode=assignment['mode'])


@router.callback_query(F.data == 'train_stop')
async def train_stop_callback(cq: CallbackQuery):
    if PG_POOL is None:
        await cq.answer()
        return
    actor = await resolve_actor(PG_POOL, cq.from_user.id)
    if actor is None:
        await cq.answer('Вы не привязаны к системе.', show_alert=True)
        return
    session = await training_simulator.get_active_session(PG_POOL, actor)
    if session is None:
        await cq.answer('Активной тренировки нет.', show_alert=True)
        return
    await cq.answer()
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        text, level = await training_simulator.end_session_early(PG_POOL, session)
    except Exception:
        log.exception('ошибка досрочного завершения, session id=%s', session['id'])
        await cq.message.answer('Не удалось завершить тренировку, попробуйте ещё раз.')
        return
    level_line = f' Уровень: {level}.' if level else ''
    await cq.message.answer(f'{text}{level_line}')


@router.message(Command('stop'))
async def stop_command(message: Message):
    """То же, что кнопка — на случай, если она уехала вверх по переписке."""
    actor = await _require_employee(message)
    if actor is None:
        return
    session = await training_simulator.get_active_session(PG_POOL, actor)
    if session is None:
        await message.answer('Активной тренировки нет. Начать: /train')
        return
    text, level = await training_simulator.end_session_early(PG_POOL, session)
    level_line = f' Уровень: {level}.' if level else ''
    await message.answer(f'{text}{level_line}')


@router.message(Command('help'))
async def help_command(message: Message):
    await message.answer(START_TEXT)


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
    if arg in ('drill', 'возражения', 'отработка'):
        await _launch(message, actor, topic=None, assigned_by=None, mode='drill')
    elif arg in ('dialog', 'разговор', 'звонок'):
        await _launch(message, actor, topic=None, assigned_by=None, mode='dialog')
    else:
        await message.answer('Что тренируем?', reply_markup=_mode_keyboard())


@router.callback_query(F.data.startswith('train_mode:'))
async def train_mode_callback(cq: CallbackQuery):
    if PG_POOL is None:
        await cq.answer()
        return
    actor = await resolve_actor(PG_POOL, cq.from_user.id)
    if actor is None:
        await cq.answer('Вы не привязаны к системе.', show_alert=True)
        return
    mode = cq.data.rsplit(':', 1)[1]
    if mode not in ('dialog', 'drill'):
        await cq.answer('Неизвестный режим.', show_alert=True)
        return
    await cq.answer()
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _launch(cq.message, actor, topic=None, assigned_by=None, mode=mode)


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
        await message.answer('Нет активной тренировки. Начать: /train')
        return
    try:
        file = await message.bot.get_file(message.voice.file_id)
        buf = await message.bot.download_file(file.file_path)
        raw_transcript, _dur = await dg_transcribe_bytes(buf.read())
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
        await message.answer('Нет активной тренировки. Начать: /train')
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
