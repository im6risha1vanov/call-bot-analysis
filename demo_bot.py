from __future__ import annotations
import asyncio, difflib, html, json, logging, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

ROOT=Path(__file__).parent
load_dotenv(ROOT/'.env')
logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'),format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('callbot')
# читают свои переменные окружения при импорте — обязательно после load_dotenv
from analysis import CRITERIA, review_call
from reports import detail_button, fmt_call_time, fmt_phone, render_head, render_manager
import rop_agent
from tools import is_privileged, resolve_actor

BOT_TOKEN=os.environ['BOT_TOKEN']
router=Router()

# Postgres + Mango: звонки приходят опросом АТС, не ручной загрузкой в чат.
PG_POOL: asyncpg.Pool | None = None


async def send_chunks(bot,chat_id,text):
    while text:
        cut=min(4000,len(text))
        if len(text)>4000: cut=max(text.rfind('\n',0,cut),1)
        await bot.send_message(chat_id,text[:cut],parse_mode='HTML'); text=text[cut:].lstrip()


async def _bind_by_username(message) -> str | None:
    """Привязка сотрудника к чату по юзернейму. Числовой id Telegram отдаёт
    боту только когда человек сам ему напишет, поэтому руководителя заводят
    строкой с telegram_username, а id проставляется здесь, при первом /start.
    Повторные /start ничего не меняют: условие telegram_user_id IS NULL."""
    username = (message.from_user.username or '').lower()
    if not username or PG_POOL is None:
        return None
    row = await PG_POOL.fetchrow(
        'UPDATE employees SET telegram_user_id=$1 '
        'WHERE lower(telegram_username)=$2 AND telegram_user_id IS NULL '
        'RETURNING full_name, role',
        message.from_user.id, username)
    if row is None:
        return None
    log.info('привязан по юзернейму @%s: %s (%s)', username, row['full_name'], row['role'])
    who = 'руководителя' if row['role'] == 'head' else 'менеджера'
    return f'Узнал вас, {html.escape(row["full_name"] or username)} — вы подключены как {who}. Отчёты будут приходить сюда.'


@router.message(CommandStart())
async def start_command(message):
    bound = await _bind_by_username(message)
    if bound:
        await message.answer(bound)
    await help_command(message)


@router.message(Command('help'))
async def help_command(message):
    await message.answer(
        'Звонки разбираются автоматически из Mango — присылать записи не нужно.\n\n'
        '<b>Команды</b>\n'
        '/check &lt;критерий&gt; [дней] — что модель увидела по критерию (руководителю)\n'
        '/assign_train &lt;добавочный&gt; [режим] — назначить тренировку в тренажёре (руководителю)\n'
        '/trainings — последние тренировки, /training &lt;номер&gt; — одна подробно\n'
        '/approvals — задачи, ждущие подтверждения (руководителю)\n\n'
        'Свободный текст — вопрос по отделу.\n'
        'id этого чата: <code>{}</code>'.format(message.chat.id),
        parse_mode='HTML')

# --------------------------------------------------------- тренажёр возражений
# Сама тренировка живёт в отдельном боте (training_bot.py).
# Здесь остаётся только назначение тренировки РОПом.

TRAIN_BOT_USERNAME = os.getenv('TRAIN_BOT_USERNAME', '').strip().lstrip('@')


@router.message(Command('assign_train'))
async def assign_train_command(message: Message):
    """Только РОП. Менеджеру уходит ссылка на бота-тренажёра: одно нажатие и
    запускает того бота (иначе Telegram не даст ему написать первым), и
    стартует назначенную тренировку."""
    if PG_POOL is None:
        return
    actor = await resolve_actor(PG_POOL, message.from_user.id)
    if actor is None or not is_privileged(actor):
        await message.answer('Команда доступна только РОПу.')
        return
    args = (message.text or '').split(maxsplit=3)
    if len(args) < 2:
        await message.answer(
            'Использование: <code>/assign_train &lt;добавочный&gt; &lt;режим&gt; [тема]</code>\n\n'
            '<b>Режимы</b>\n'
            '• <code>разговор</code> — холодный звонок целиком, до 20 реплик\n'
            '• <code>возражения</code> — отработка возражений поштучно, с разбором каждого ответа\n\n'
            'Например: <code>/assign_train 13 возражения</code>', parse_mode='HTML')
        return
    extension = args[1]
    mode, topic = 'dialog', None
    if len(args) > 2:
        raw_mode = args[2].lower()
        if raw_mode in ('возражения', 'отработка', 'drill'):
            mode = 'drill'
            topic = args[3] if len(args) > 3 else None
        elif raw_mode in ('разговор', 'звонок', 'dialog'):
            topic = args[3] if len(args) > 3 else None
        else:
            # Режим не указан — значит всё после добавочного это тема,
            # как было до появления второго режима.
            topic = ' '.join(args[2:])
    manager = await PG_POOL.fetchrow(
        "SELECT * FROM employees WHERE client_id=$1 AND extension=$2 AND role='manager'",
        actor.client_id, extension,
    )
    if not manager:
        await message.answer(f'Менеджер с добавочным {html.escape(extension)} не найден.')
        return
    if not manager['telegram_user_id']:
        await message.answer(f'Менеджер (доб. {html.escape(extension)}) ещё не подключён к боту.')
        return
    assignment_id = await PG_POOL.fetchval(
        """INSERT INTO pending_train_assignments (client_id, extension, topic, mode,
                                                   assigned_by_telegram_user_id)
           VALUES ($1,$2,$3,$4,$5) RETURNING id""",
        actor.client_id, extension, topic, mode, actor.telegram_user_id,
    )
    if not TRAIN_BOT_USERNAME:
        await message.answer('Не задан TRAIN_BOT_USERNAME в .env — ссылку на тренажёр не собрать.')
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Начать тренировку',
                              url=f'https://t.me/{TRAIN_BOT_USERNAME}?start=train_{assignment_id}')
    ]])
    mode_line = 'отработка возражений' if mode == 'drill' else 'разговор целиком'
    topic_line = f', тема «{html.escape(topic)}»' if topic else ''
    try:
        await message.bot.send_message(
            manager['telegram_user_id'],
            f'РОП назначил вам тренировку: {mode_line}{topic_line}. Она пройдёт в боте-тренажёре.',
            reply_markup=kb)
    except Exception:
        log.exception('не удалось отправить уведомление о тренировке, доб.=%s', extension)
        await message.answer('Не удалось отправить уведомление менеджеру (возможно, он не запускал бота).')
        return
    await message.answer(
        f'Назначено ({mode_line}), доб. {html.escape(extension)} получит ссылку на тренажёр.')


# ------------------------------------------------ проверка критерия руками

CHECK_DEFAULT_CALLS = 10
CHECK_MAX_CALLS = 30


# Слова, которыми критерий называют в разговоре, но которых нет в его
# формулировке: «ЛПР» в названии критерия не встречается, а спросят именно так.
CHECK_ALIASES = {
    'лпр': 'decision_influence',
    'цпр': 'decision_influence',
    'инсайт': 'insight',
    'монолог': 'talk_share',
    'болтал': 'talk_share',
    'перебивал': 'talk_share',
}


def _match_criterion(query: str) -> tuple[str, str] | None:
    """Ищем критерий по куску названия, по ключу или по разговорному синониму:
    руководитель пишет «извлекающие» или «ЛПР», а не implication_questions."""
    q = query.strip().lower()
    if not q:
        return None
    if q in CHECK_ALIASES:
        key = CHECK_ALIASES[q]
        return next((key, t) for k, _w, t in CRITERIA if k == key)
    for key, _w, title in CRITERIA:
        if q == key.lower() or q == title.lower():
            return key, title
    for key, _w, title in CRITERIA:
        if q in title.lower() or q in key.lower():
            return key, title
    titles = {title.lower(): (key, title) for key, _w, title in CRITERIA}
    close = difflib.get_close_matches(q, list(titles), n=1, cutoff=0.5)
    return titles[close[0]] if close else None


def _criteria_list() -> str:
    return '\n'.join(f'• {html.escape(title)}' for _k, _w, title in CRITERIA)


def _render_check(rows, key: str, title: str, days: int | None, client_tz: str) -> str:
    """Собирает ответ /check. Вынесено из обработчика, чтобы вывод можно было
    проверить на реальных данных, не поднимая Telegram."""
    header = f'<b>{html.escape(title)}</b>\n'
    header += f'за последние {days} дн.' if days else f'последние {len(rows)} звонков'

    lines, applicable, failed = [], 0, 0
    for r in rows:
        analysis = json.loads(r['analysis'])
        row = next((x for x in (analysis.get('rows') or []) if x.get('key') == key), None)
        name = r['full_name'] or f"доб. {r['extension']}"
        # У части звонков время начала не пришло от Манго — fmt_call_time
        # вернёт None, и без запасного значения команда падала бы у РОПа в чате.
        when = fmt_call_time(r['call_started_at'], client_tz) or 'время неизвестно'
        if row is None or not row.get('applicable'):
            mark = '— неприменим'
        else:
            applicable += 1
            if row.get('passed'):
                mark = '✅ пройден'
            else:
                failed += 1
                mark = '❌ провален'
        evidence = (row or {}).get('evidence') or '(обоснование не записано)'
        # Звонок опознаём по номеру клиента: по нему сотрудник находит запись в
        # Манго. Внутренний номер записи в базе для этого бесполезен — в
        # интерфейсе Манго его нет.
        phone = fmt_phone(r['client_number']) or 'номер неизвестен'
        lines.append(
            f"\n\n<b>{html.escape(phone)}</b> · {html.escape(when)} · {html.escape(name)} · {mark}\n"
            f"<i>{html.escape(str(evidence)[:400])}</i>"
        )

    summary = f'\n\nПровален в {failed} из {applicable} применимых.'
    if applicable and failed == applicable:
        summary += ('\nКритерий не различает менеджеров. Если обоснования выглядят как '
                    '«не было попытки» — критерий верен, и учить надо команду. Если модель '
                    'отвергает то, что по сути засчитывается, — стоит поправить формулировку '
                    'критерия (это решение человека, бот её не меняет).')
    return header + ''.join(lines) + summary


@router.message(Command('check'))
async def check_command(message: Message):
    """Показывает, ЧТО именно модель увидела по критерию: свой вердикт и
    обоснование (evidence) по последним звонкам. Нужна, чтобы отличить
    «менеджеры правда так работают» от «модель придирается к формулировке» —
    на слух это проверять слишком долго. Формулировки критериев по итогам
    проверки меняет человек, не бот."""
    if PG_POOL is None:
        return
    actor = await resolve_actor(PG_POOL, message.from_user.id)
    if actor is None or not is_privileged(actor):
        await message.answer('Команда доступна только руководителю.')
        return

    args = (message.text or '').split()[1:]
    days = None
    if args and args[-1].isdigit():
        days = int(args[-1])
        args = args[:-1]
    matched = _match_criterion(' '.join(args))
    if matched is None:
        await message.answer(
            'Использование: <code>/check &lt;критерий&gt; [дней]</code>\n'
            'Например: <code>/check извлекающие</code> или <code>/check отговорка 7</code>\n\n'
            '<b>Критерии</b>\n' + _criteria_list(), parse_mode='HTML')
        return
    key, title = matched

    cutoff = None
    limit = CHECK_DEFAULT_CALLS
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        limit = CHECK_MAX_CALLS

    rows = await PG_POOL.fetch(
        """
        SELECT a.call_id, a.analysis, a.level, c.client_number, c.call_started_at, c.extension, e.full_name
        FROM astra_analysis a
        JOIN calls c ON c.id = a.call_id
        LEFT JOIN employees e ON e.client_id = c.client_id AND e.extension = c.extension
                              AND e.role = 'manager'
        WHERE a.status = 'analyzed' AND a.analysis IS NOT NULL AND c.client_id = $1
          AND ($2::timestamptz IS NULL OR c.call_started_at >= $2)
        -- NULLS LAST обязателен: у части звонков Манго не отдала время начала,
        -- а в Postgres при DESC пустые идут первыми — «последние звонки»
        -- оказались бы как раз теми, у которых времени нет.
        ORDER BY c.call_started_at DESC NULLS LAST
        LIMIT $3
        """,
        actor.client_id, cutoff, limit,
    )
    if not rows:
        await message.answer('Разобранных звонков за этот период нет.')
        return

    client_tz = await PG_POOL.fetchval('SELECT timezone FROM clients WHERE id=$1', actor.client_id)
    await send_chunks(message.bot, message.chat.id, _render_check(rows, key, title, days, client_tz))


# ------------------------------------------------- просмотр тренировок

TRAININGS_DEFAULT = 10


def _training_result(row) -> str:
    """Разговор мерится уровнем (той же линейкой, что и реальные звонки),
    отработка возражений — долей зачтённых ответов: уровень звонка к набору
    отговорок неприменим."""
    if row['status'] == 'active':
        return 'идёт'
    if row['status'] != 'completed':
        return row['status']
    if row['mode'] == 'drill':
        state = json.loads(row['drill_state']) if row['drill_state'] else {}
        results = state.get('results') or []
        return f"зачтено {sum(1 for r in results if r.get('passed'))} из {len(results)}"
    return row['level'] or 'без оценки'


@router.message(Command('trainings'))
async def trainings_command(message: Message):
    """Список тренировок. Руководитель видит весь отдел, менеджер — только свои
    (права как везде: решает код по Actor, не текст запроса)."""
    if PG_POOL is None:
        return
    actor = await resolve_actor(PG_POOL, message.from_user.id)
    if actor is None:
        return
    args = (message.text or '').split()[1:]
    requested_ext = args[0] if args else None
    extension = actor.extension if actor.role == 'manager' else requested_ext

    rows = await PG_POOL.fetch(
        """
        SELECT t.id, t.extension, t.mode, t.status, t.level, t.score, t.turns_count,
               t.drill_state, t.started_at, t.is_test, e.full_name
        FROM training_sessions t
        LEFT JOIN employees e ON e.client_id = t.client_id AND e.extension = t.extension
        WHERE t.client_id = $1 AND ($2::text IS NULL OR t.extension = $2)
        ORDER BY t.started_at DESC LIMIT $3
        """,
        actor.client_id, extension, TRAININGS_DEFAULT,
    )
    if not rows:
        await message.answer('Тренировок пока не было.')
        return

    client_tz = await PG_POOL.fetchval('SELECT timezone FROM clients WHERE id=$1', actor.client_id)
    lines = ['<b>Последние тренировки</b>']
    for r in rows:
        who = r['full_name'] or (f"доб. {r['extension']}" if r['extension'] else 'руководитель')
        when = fmt_call_time(r['started_at'], client_tz) or '—'
        mode = 'возражения' if r['mode'] == 'drill' else 'разговор'
        test = ' · пробная' if r['is_test'] else ''
        lines.append(f"<b>#{r['id']}</b> · {html.escape(when)} · {html.escape(who)} · "
                     f"{mode} · {html.escape(_training_result(r))}{test}")
    lines.append('\nПодробности одной тренировки: <code>/training &lt;номер&gt;</code>')
    await send_chunks(message.bot, message.chat.id, '\n'.join(lines))


@router.message(Command('training'))
async def training_detail_command(message: Message):
    """Что именно отвечал человек и как это оценила модель — то, ради чего
    руководителю и нужен просмотр: увидеть не только балл, но и основания."""
    if PG_POOL is None:
        return
    actor = await resolve_actor(PG_POOL, message.from_user.id)
    if actor is None:
        return
    args = (message.text or '').split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer('Использование: <code>/training &lt;номер&gt;</code> — номер из /trainings',
                              parse_mode='HTML')
        return

    row = await PG_POOL.fetchrow(
        """
        SELECT t.*, e.full_name FROM training_sessions t
        LEFT JOIN employees e ON e.client_id = t.client_id AND e.extension = t.extension
        WHERE t.id = $1
        """,
        int(args[1]),
    )
    if row is None or row['client_id'] != actor.client_id:
        await message.answer('Тренировка не найдена.')
        return
    if actor.role == 'manager' and row['extension'] != actor.extension:
        await message.answer('Это тренировка другого сотрудника.')
        return

    client_tz = await PG_POOL.fetchval('SELECT timezone FROM clients WHERE id=$1', actor.client_id)
    who = row['full_name'] or (f"доб. {row['extension']}" if row['extension'] else 'руководитель')
    mode = 'отработка возражений' if row['mode'] == 'drill' else 'разговор целиком'
    out = [f"<b>Тренировка #{row['id']}</b>",
           f"{html.escape(who)} · {mode} · {html.escape(fmt_call_time(row['started_at'], client_tz) or '—')}",
           f"Итог: {html.escape(_training_result(row))}"]
    if row['is_test']:
        out.append('<i>Пробная сессия руководителя — в статистику отдела не входит.</i>')

    if row['mode'] == 'drill':
        state = json.loads(row['drill_state']) if row['drill_state'] else {}
        for i, r in enumerate((state.get('results') or []), 1):
            mark = '✅' if r.get('passed') else '❌'
            out.append(f"\n<b>{i}. Клиент:</b> {html.escape(r['objection'])}"
                       f"\n<b>Ответ:</b> {html.escape(str(r['answer'])[:600])}"
                       f"\n{mark} {html.escape(str(r.get('comment') or ''))}")
    else:
        for turn in json.loads(row['transcript']):
            speaker = 'Клиент' if turn['role'] == 'client' else 'Менеджер'
            out.append(f"\n<b>{speaker}:</b> {html.escape(str(turn['text'])[:600])}")

    await send_chunks(message.bot, message.chat.id, '\n'.join(out))


# --------------------------------------------------- очередь на подтверждение

def _approval_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='✅ Утвердить', callback_data=f'approve:{task_id}'),
        InlineKeyboardButton(text='✖️ Отклонить', callback_data=f'reject:{task_id}'),
    ]])


@router.message(Command('approvals'))
async def approvals_command(message: Message):
    """Список задач, ждущих решения человека. Всё, что выходит за пределы нашей
    базы — отправка наружу, трата денег, публикация — раннер оставляет в
    awaiting_approval, и выполняется это только после нажатия здесь."""
    if PG_POOL is None:
        return
    actor = await resolve_actor(PG_POOL, message.from_user.id)
    if actor is None or not is_privileged(actor):
        await message.answer('Команда доступна только руководителю.')
        return
    rows = await PG_POOL.fetch(
        """SELECT id, type, result, created_at FROM tasks
           WHERE status='awaiting_approval' AND (client_id IS NULL OR client_id=$1)
           ORDER BY id LIMIT 10""",
        actor.client_id,
    )
    if not rows:
        await message.answer('Ничего не ждёт подтверждения.')
        return
    for r in rows:
        summary = ''
        if r['result']:
            summary = (json.loads(r['result']) or {}).get('awaiting', '')
        await message.answer(
            f"Задача #{r['id']} · {html.escape(r['type'])}\n{html.escape(summary or 'без описания')}",
            reply_markup=_approval_keyboard(r['id']),
        )


async def _resolve_approval(cq: CallbackQuery, approve: bool) -> None:
    if PG_POOL is None:
        return
    actor = await resolve_actor(PG_POOL, cq.from_user.id)
    if actor is None or not is_privileged(actor):
        await cq.answer('Только руководитель может подтверждать.', show_alert=True)
        return
    task_id = int(cq.data.split(':')[1])
    # Условие на статус — защита от двойного нажатия: второй клик не найдёт
    # строку в awaiting_approval и ничего не сделает.
    if approve:
        updated = await PG_POOL.execute(
            """UPDATE tasks SET status='new', run_after=now(), approved_by=$2, approved_at=now(),
                      updated_at=now()
               WHERE id=$1 AND status='awaiting_approval'""",
            task_id, cq.from_user.id,
        )
    else:
        updated = await PG_POOL.execute(
            """UPDATE tasks SET status='skipped', approved_by=$2, approved_at=now(), updated_at=now()
               WHERE id=$1 AND status='awaiting_approval'""",
            task_id, cq.from_user.id,
        )
    if updated == 'UPDATE 0':
        await cq.answer('Задача уже обработана.', show_alert=True)
        return
    verdict = 'утверждена — уйдёт в работу' if approve else 'отклонена'
    await cq.message.edit_text(f"{cq.message.text}\n\n— {verdict}")
    await cq.answer()


@router.callback_query(F.data.startswith('approve:'))
async def approve_callback(cq: CallbackQuery):
    await _resolve_approval(cq, approve=True)


@router.callback_query(F.data.startswith('reject:'))
async def reject_callback(cq: CallbackQuery):
    await _resolve_approval(cq, approve=False)


# ------------------------------------------------------------ агент РОПа

@router.message(F.text)
async def rop_question(message: Message):
    """Свободный вопрос агенту РОПа (Этап 3). Регистрируется после команд —
    aiogram отдаёт сообщение сюда, только если ни один Command()/CommandStart()
    фильтр выше не совпал. Actor резолвится по telegram_user_id из employees;
    если человек не сотрудник ни одного клиента — молчим, а не отвечаем как
    попало."""
    if PG_POOL is None:
        return
    actor = await resolve_actor(PG_POOL, message.from_user.id)
    if actor is None:
        return

    status = await message.answer('Секунду, смотрю данные…')
    try:
        text = await rop_agent.answer(PG_POOL, actor, message.text or '')
    except Exception:
        log.exception('ошибка агента РОПа, telegram_user_id=%s', message.from_user.id)
        text = 'Не получилось обработать вопрос, попробуйте ещё раз.'
    try:
        await status.delete()
    except Exception:
        pass
    await send_chunks(message.bot, message.chat.id, text)

@router.message(F.voice|F.audio|F.document)
async def audio_message(message):
    await message.answer('Записи вручную больше не принимаются. Разбор идёт по звонкам из Mango.')

@router.callback_query(F.data.startswith('detail:astra:'))
async def detail_callback(cq: CallbackQuery):
    """«Подробный разбор» звонка из Mango. Права проверяются в момент
    клика по таблице employees; результат кешируется в astra_analysis
    (второй клик — без нового запроса к модели)."""
    try:
        call_id = int(cq.data.rsplit(':', 1)[1])
    except (ValueError, IndexError):
        await cq.answer('Некорректные данные кнопки.', show_alert=True); return

    call = await PG_POOL.fetchrow(
        'SELECT c.*, a.analysis, a.detailed_report, a.cost_units FROM calls c '
        'JOIN astra_analysis a ON a.call_id = c.id WHERE c.id=$1', call_id
    )
    if not call:
        await cq.answer('Звонок не найден.', show_alert=True); return

    client_tz = await PG_POOL.fetchval('SELECT timezone FROM clients WHERE id=$1', call['client_id'])
    call_time = fmt_call_time(call['call_started_at'], client_tz)

    employee = await PG_POOL.fetchrow(
        'SELECT * FROM employees WHERE client_id=$1 AND telegram_user_id=$2',
        call['client_id'], cq.from_user.id,
    )
    is_head_viewer = bool(employee) and employee['role'] == 'head'
    is_own_manager = bool(employee) and employee['role'] == 'manager' and employee['extension'] == call['extension']
    if not (is_head_viewer or is_own_manager):
        await cq.answer('Доступ к разбору этого звонка закрыт.', show_alert=True); return

    if not call['analysis']:
        await cq.answer('Нет данных для разбора — звонок был слишком коротким.', show_alert=True); return

    analysis = json.loads(call['analysis'])
    rows = analysis.get('rows') or []
    scores = {k: v for k, v in analysis.items() if k != 'rows'}

    if call['detailed_report']:
        await cq.answer()
        detailed = json.loads(call['detailed_report'])
    else:
        await cq.answer('Готовлю подробный разбор, это займёт около минуты…')
        try:
            detailed, _cost = await review_call(call['transcript'] or '', scores, rows)
        except Exception:
            log.exception('не удалось построить подробный разбор (Astra), call id=%s', call_id)
            await cq.bot.send_message(cq.from_user.id, 'Не удалось построить разбор, попробуйте ещё раз позже.')
            return
        await PG_POOL.execute(
            'UPDATE astra_analysis SET detailed_report=$2::jsonb, detailed_report_requested_at=now() WHERE call_id=$1',
            call_id, json.dumps(detailed, ensure_ascii=False),
        )
        try:
            await cq.message.edit_reply_markup(reply_markup=detail_button(call_id, source='astra', ready=True))
        except Exception:
            pass

    full = {**scores, **detailed, 'rows': rows}
    if is_head_viewer:
        manager = await PG_POOL.fetchrow(
            "SELECT full_name FROM employees WHERE client_id=$1 AND extension=$2 AND role='manager'",
            call['client_id'], call['extension'],
        )
        manager_name = (manager['full_name'] if manager else None) or f"доб. {call['extension']}"
        text = render_head(full, manager_name, call['duration_seconds'] or 0, float(call['cost_units'] or 0), '(Astra)',
                               call_time=call_time)
    else:
        text = render_manager(full, call['duration_seconds'] or 0, call_time=call_time)
    await send_chunks(cq.bot, cq.from_user.id, text)


async def main():
    global PG_POOL
    # Режим форматирования задаём один раз на бота: в aiogram 3.7+ его убрали
    # из Bot(token, parse_mode=...) в DefaultBotProperties, и при обновлении
    # библиотеки настройка потерялась — теги <b> стали уезжать в чат текстом.
    # Динамические куски по всему файлу экранируются html.escape/esc.
    bot=Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
    dp=Dispatcher(); dp.include_router(router)
    PG_POOL = await asyncpg.create_pool(os.environ['DATABASE_URL'], min_size=1, max_size=5)
    try: await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close(); await PG_POOL.close()

if __name__=='__main__': asyncio.run(main())
