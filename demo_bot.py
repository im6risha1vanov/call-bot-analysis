from __future__ import annotations
import asyncio, difflib, html, json, logging, os, shutil, sqlite3, subprocess, tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import httpx
import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

ROOT=Path(__file__).parent
load_dotenv(ROOT/'.env')
logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'),format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('callbot')
# читают свои переменные окружения при импорте — обязательно после load_dotenv
from analysis import analyze, review_call
# алиасы: у этого файла уже есть свои render_head/render_manager (старый ручной
# аплоад) — импорт под своими именами их бы тихо подменил
from reports import detail_button, fmt_call_time, render_head as pg_render_head, render_manager as pg_render_manager
import rop_agent
# training_simulator/tts/Deepgram-распознавание переехали в training_bot.py —
# тренажёр живёт в отдельном боте, здесь они больше не нужны.
from tools import resolve_actor

BOT_TOKEN=os.environ['BOT_TOKEN']; DG_KEY=os.environ['DEEPGRAM_API_KEY']
DB_PATH=ROOT/os.getenv('DATABASE_PATH','callbot.sqlite3')
_head=os.getenv('HEAD_CHAT_ID','').strip(); HEAD_CHAT_ID=int(_head) if _head else None
MIN_SECONDS=float(os.getenv('MIN_CALL_SECONDS','35')); MAX_FILE=int(os.getenv('TELEGRAM_MAX_DOWNLOAD_BYTES','20971520')); MAX_COST=float(os.getenv('MAX_COST_PER_CHAT_UNITS','2000000')); RESERVE=float(os.getenv('MAX_ESTIMATED_ANALYSIS_COST_UNITS','20000')); DG_RATE=float(os.getenv('DEEPGRAM_USD_PER_MINUTE','.0077'))
router=Router(); queue=asyncio.Queue(); db_lock=asyncio.Lock()

# Postgres — общая база с Claude-ботом (/opt/callbot), нужна только для
# нового конвейера сравнения (astra_worker.py + кнопка «Подробный разбор»);
# старый ручной аплоад выше по файлу продолжает жить в SQLite отдельно.
PG_POOL: asyncpg.Pool | None = None

@dataclass
class Job:
    chat_id:int; progress_id:int; file_id:str; filename:str; manager:str

def connect():
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; return c

def init_db():
    with connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY, chat_id INTEGER, manager TEXT, duration_seconds REAL, score INTEGER, analysis_json TEXT, transcript TEXT, cost_usd REAL, created_at TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS managers(name_key TEXT PRIMARY KEY, display_name TEXT, chat_id INTEGER, registered_at TEXT)')

def mime_type(name):
    return {'.mp3':'audio/mpeg','.mpeg':'audio/mpeg','.wav':'audio/wav','.ogg':'audio/ogg','.opus':'audio/ogg','.m4a':'audio/mp4','.mp4':'audio/mp4','.webm':'audio/webm','.flac':'audio/flac','.aac':'audio/aac'}.get(Path(name).suffix.lower(),'application/octet-stream')

def probe(path):
    r=subprocess.run(['ffprobe','-v','error','-select_streams','a:0','-show_entries','format=duration:stream=channels','-of','json',str(path)],capture_output=True,text=True)
    if r.returncode: raise RuntimeError(r.stderr.strip() or 'ffprobe не прочитал аудио')
    data=json.loads(r.stdout); return float(data['format']['duration']),int((data.get('streams') or [{}])[0].get('channels',1))

def split_stereo(source, folder):
    left,right=folder/'speaker0.wav',folder/'speaker1.wav'
    # -map_channel убран в ffmpeg 8.x; channelsplit — актуальная замена.
    r=subprocess.run(['ffmpeg','-y','-v','error','-i',str(source),'-filter_complex','channelsplit=channel_layout=stereo[left][right]','-map','[left]',str(left),'-map','[right]',str(right)],capture_output=True,text=True)
    if r.returncode: raise RuntimeError(r.stderr.strip() or 'ffmpeg не смог разделить стерео')
    return left,right

async def deepgram(path, diarize):
    async with httpx.AsyncClient(timeout=180) as client:
        r=await client.post('https://api.deepgram.com/v1/listen',params={'model':'nova-3','language':'ru','smart_format':'true','utterances':'true','diarize':str(diarize).lower()},headers={'Authorization':'Token '+DG_KEY,'Content-Type':mime_type(path.name)},content=path.read_bytes())
    if r.status_code>=300:
        log.error('Deepgram %s: %s',r.status_code,r.text[:4000]); raise RuntimeError('Deepgram '+str(r.status_code)+': '+r.text[:700])
    return r.json()

def utterances(data, label=None):
    result=[]
    for u in data.get('results',{}).get('utterances',[]):
        text=u.get('transcript','').strip()
        if text: result.append((float(u.get('start',0)),'[{:.1f}-{:.1f}] {}: {}'.format(u.get('start',0),u.get('end',0),label or 'Спикер '+str(u.get('speaker',0)),text)))
    return result

async def transcribe(path: str) -> tuple[str,float]:
    source=Path(path); duration,channels=await asyncio.to_thread(probe,source)
    if channels==2:
        left,right=await asyncio.to_thread(split_stereo,source,source.parent)
        a,b=await asyncio.gather(deepgram(left,False),deepgram(right,False))
        return '\n'.join(text for _,text in sorted(utterances(a,'Спикер 0')+utterances(b,'Спикер 1'))),duration
    return '\n'.join(text for _,text in utterances(await deepgram(source,True))),duration

async def update(bot,job,text):
    try: await bot.edit_message_text(text[:4090],job.chat_id,job.progress_id)
    except Exception: pass

async def total_spent(chat_id):
    async with db_lock:
        with connect() as c: return float(c.execute('SELECT COALESCE(SUM(cost_usd),0) FROM calls WHERE chat_id=?',(chat_id,)).fetchone()[0])

async def send_chunks(bot,chat_id,text):
    while text:
        cut=min(4000,len(text))
        if len(text)>4000: cut=max(text.rfind('\n',0,cut),1)
        await bot.send_message(chat_id,text[:cut],parse_mode='HTML'); text=text[cut:].lstrip()

# ------------------------------------------------------------- реестр менеджеров

def normalize_name(s):
    return ' '.join(s.lower().split())

async def register_manager(name, chat_id):
    key=normalize_name(name)
    if not key: return
    async with db_lock:
        with connect() as c:
            c.execute('INSERT INTO managers(name_key,display_name,chat_id,registered_at) VALUES(?,?,?,?) '
                      'ON CONFLICT(name_key) DO UPDATE SET display_name=excluded.display_name, chat_id=excluded.chat_id, registered_at=excluded.registered_at',
                      (key,name.strip(),chat_id,datetime.now(timezone.utc).isoformat()))

async def find_manager_chat(name):
    key=normalize_name(name)
    if not key: return None
    async with db_lock:
        with connect() as c: rows=c.execute('SELECT name_key,display_name,chat_id FROM managers').fetchall()
    if not rows: return None
    for r in rows:
        if r['name_key']==key: return r['chat_id'],r['display_name']
    for r in rows:
        if key in r['name_key'] or r['name_key'] in key: return r['chat_id'],r['display_name']
    close=difflib.get_close_matches(key,[r['name_key'] for r in rows],n=1,cutoff=0.6)
    if close:
        r=next(r for r in rows if r['name_key']==close[0]); return r['chat_id'],r['display_name']
    return None

# ------------------------------------------------------------------ отчёты

def unquote(s):
    s=str(s).strip()
    return s[1:-1].strip() if len(s)>1 and s[0] in '«"' and s[-1] in '»"' else s

SEVERITY_ICON={'критично':'🔴','заметно':'🟡','мелочь':'⚪'}

def render_head(result,manager,duration,cost,manager_status):
    score=result.get('score'); score_str='{}/100'.format(score) if score is not None else 'нет данных'
    fh=result.get('for_head') or {}
    headline=result.get('headline')
    lines=['<b>Разбор звонка</b> · {} · {:.1f} мин · <b>{}</b>'.format(html.escape(manager),duration/60,score_str)]
    lines.append(html.escape(unquote(headline)) if headline else '—')
    if result.get('call_cut_short'): lines.append('⚠️ Звонок короткий или транскрипт обрывочный — данных мало')
    meeting=('встреча: '+html.escape(str(result.get('meeting_details') or 'да, время не уточнено'))) if result.get('meeting_booked') else 'встреча НЕ назначена'
    lines+=['','<b>Итог:</b> '+meeting,'<b>Влияет на решение:</b> '+('да' if result.get('influences_decision') else 'нет/не выяснено')]
    if result.get('gatekeeper_present'): lines.append('<b>Секретарь:</b> был в разговоре')
    cliches=result.get('cliches') or []
    if cliches: lines+=['','<b>Штампы:</b> '+', '.join('«'+html.escape(str(c))+'»' for c in cliches)]
    if fh.get('narrative'): lines+=['','<b>Как прошёл разговор</b>',html.escape(str(fh['narrative']))]
    diag=fh.get('diagnosis') or {}
    if diag.get('type'):
        lines+=['','<b>Диагноз:</b> '+html.escape(str(diag['type']))]
        if diag.get('reasoning'): lines.append(html.escape(str(diag['reasoning'])))
    tp=fh.get('turning_point') or {}
    if tp.get('quote') or tp.get('what_happened'):
        lines+=['','<b>Поворотный момент</b>']
        if tp.get('quote'): lines.append('«'+html.escape(unquote(tp['quote']))+'»')
        if tp.get('what_happened'): lines.append(html.escape(str(tp['what_happened'])))
        if tp.get('alternative'): lines.append('Стоило: '+html.escape(str(tp['alternative'])))
    gaps=fh.get('skill_gaps') or []
    if gaps:
        lines+=['','<b>Пробелы в навыках</b>']
        for g in gaps:
            icon=SEVERITY_ICON.get(g.get('severity'),'•')
            lines.append('{} {}'.format(icon,html.escape(str(g.get('skill','')))))
            if g.get('evidence'): lines.append('   «'+html.escape(unquote(g['evidence']))+'»')
            if g.get('recurring_risk'): lines.append('   Риск: '+html.escape(str(g['recurring_risk'])))
    if fh.get('lead_quality'): lines+=['','<b>Качество лида:</b> '+html.escape(str(fh['lead_quality']))]
    if fh.get('lead_salvageable'): lines.append('<b>Можно спасти:</b> '+html.escape(str(fh.get('salvage_action') or 'да')))
    if fh.get('coaching_action'): lines+=['','<b>Действие на этой неделе:</b> '+html.escape(str(fh['coaching_action']))]
    lines+=['',manager_status,'Стоимость: '+format(cost,'.0f')+' ед. (Astra) + Deepgram $'+format(duration/60*DG_RATE,'.4f')]
    return '\n'.join(lines)

def render_manager(result,duration):
    score=result.get('score'); score_str='{}/100'.format(score) if score is not None else 'нет данных'
    fm=result.get('for_manager') or {}
    headline=result.get('headline')
    lines=['<b>Разбор твоего звонка</b> · {:.1f} мин · <b>{}</b>'.format(duration/60,score_str)]
    if headline: lines.append(html.escape(unquote(headline)))
    if result.get('call_cut_short'): lines.append('⚠️ Запись короткая или обрывочная — разбор частичный')
    if fm.get('opening'): lines+=['',html.escape(str(fm['opening']))]
    if result.get('meeting_booked'): lines+=['','✅ Встреча назначена: '+html.escape(str(result.get('meeting_details') or ''))]
    did_well=fm.get('did_well') or []
    if did_well:
        lines+=['','<b>Что получилось</b>']
        for d in did_well:
            lines.append('• '+html.escape(str(d.get('what',''))))
            if d.get('quote'): lines.append('   «'+html.escape(unquote(d['quote']))+'»')
            if d.get('why'): lines.append('   '+html.escape(str(d['why'])))
    moments=fm.get('moments') or []
    if moments:
        lines+=['','<b>Разбор моментов</b>']
        for m in moments:
            lines.append('«'+html.escape(unquote(m.get('quote','')))+'»')
            if m.get('reaction'): lines.append('→ '+html.escape(str(m['reaction'])))
            if m.get('why'): lines.append('Почему: '+html.escape(str(m['why'])))
            if m.get('say_instead'): lines.append('Сказать вместо: «'+html.escape(unquote(m['say_instead']))+'»')
            lines.append('')
    if fm.get('key_mistake'): lines.append('<b>Главная ошибка:</b> '+html.escape(str(fm['key_mistake'])))
    if fm.get('practice'): lines.append('<b>Тренировка:</b> '+html.escape(str(fm['practice'])))
    if fm.get('next_call_focus'): lines.append('<b>Фокус на ближайшие звонки:</b> '+html.escape(str(fm['next_call_focus'])))
    return '\n'.join(lines)

async def process(bot,job):
    folder=Path(tempfile.mkdtemp(prefix='callbot-')); path=folder/job.filename
    try:
        await update(bot,job,'1/4 Скачиваю запись…'); file=await bot.get_file(job.file_id); await bot.download_file(file.file_path,destination=path)
        duration,_=await asyncio.to_thread(probe,path)
        if duration<MIN_SECONDS: await update(bot,job,'Запись короче фильтра; API не вызваны.'); return
        if await total_spent(job.chat_id)+duration/60*DG_RATE+RESERVE>MAX_COST: await update(bot,job,'Лимит расходов чата исчерпан.'); return
        await update(bot,job,'2/4 Распознаю речь…'); transcript,duration=await transcribe(str(path))
        if not transcript: raise RuntimeError('Речь не распознана')
        await update(bot,job,'3/4 Astra оценивает звонок…'); result,astra_units=await analyze(transcript); cost=astra_units+duration/60*DG_RATE
        async with db_lock:
            with connect() as c: c.execute('INSERT INTO calls(chat_id,manager,duration_seconds,score,analysis_json,transcript,cost_usd,created_at) VALUES(?,?,?,?,?,?,?,?)',(job.chat_id,job.manager,duration,result.get('score'),json.dumps(result,ensure_ascii=False),transcript,cost,datetime.now(timezone.utc).isoformat()))

        await update(bot,job,'4/4 Отправляю отчёт…')
        match=await find_manager_chat(job.manager)
        if match: manager_status='Личный разбор отправлен: '+html.escape(match[1])
        else: manager_status='Менеджер «'+html.escape(job.manager)+'» не зарегистрирован (/iam) — личный разбор не отправлен.'

        if HEAD_CHAT_ID:
            try: await send_chunks(bot,HEAD_CHAT_ID,render_head(result,job.manager,duration,cost,manager_status))
            except Exception: log.exception('Failed to deliver head report')
        if match:
            try: await send_chunks(bot,match[0],render_manager(result,duration))
            except Exception: log.exception('Failed to deliver manager report')

        await update(bot,job,'Готово ✅ Балл: {}. {}'.format(result.get('score','—'),manager_status))
    except Exception as exc:
        log.exception('Call processing failed'); await update(bot,job,'Не удалось обработать: '+str(exc)[:800])
    finally: shutil.rmtree(folder,ignore_errors=True)

async def worker(bot):
    while True:
        job=await queue.get()
        try: await process(bot,job)
        finally: queue.task_done()

@router.message(CommandStart())
@router.message(Command('help'))
async def help_command(message):
    await message.answer(
        'Пришлите аудио до 20 МБ. Первая строка подписи — фамилия менеджера.\n\n'
        '<b>Команды</b>\n'
        '/iam Фамилия — привязать этот чат к себе: личные разборы твоих звонков будут приходить сюда\n'
        '/stats — сводка по менеджерам\n'
        '/reset — очистить статистику этого чата\n\n'
        'chat_id этого чата: <code>{}</code> (пригодится для HEAD_CHAT_ID в .env)'.format(message.chat.id),
        parse_mode='HTML')

@router.message(Command('iam'))
async def iam_command(message):
    name=(message.text or '').partition(' ')[2].strip()
    if not name: await message.answer('Использование: /iam Фамилия — например, /iam Иванов'); return
    await register_manager(name,message.chat.id)
    await message.answer('Готово, '+html.escape(name)+'. Личные разборы твоих звонков теперь приходят сюда.')

@router.message(Command('reset'))
async def reset_command(message):
    async with db_lock:
        with connect() as c: c.execute('DELETE FROM calls WHERE chat_id=?',(message.chat.id,))
    await message.answer('Статистика этого чата очищена.')

@router.message(Command('stats'))
async def stats_command(message):
    async with db_lock:
        with connect() as c:
            rows=c.execute('SELECT manager,COUNT(*) n,AVG(score) avg FROM calls WHERE chat_id=? AND score IS NOT NULL GROUP BY manager ORDER BY avg DESC',(message.chat.id,)).fetchall()
            analyses=c.execute('SELECT analysis_json FROM calls WHERE chat_id=?',(message.chat.id,)).fetchall()
            cost=float(c.execute('SELECT COALESCE(SUM(cost_usd),0) FROM calls WHERE chat_id=?',(message.chat.id,)).fetchone()[0])
    if not analyses: await message.answer('Статистика пока пуста.'); return
    meetings=0; talk_shares=[]; brush_unhandled={}; cliches_count={}; total=len(analyses)
    for row in analyses:
        d=json.loads(row['analysis_json'])
        if d.get('meeting_booked'): meetings+=1
        if isinstance(d.get('manager_talk_share'),(int,float)): talk_shares.append(d['manager_talk_share'])
        for b in d.get('brush_offs') or []:
            if not b.get('handled'):
                key=str(b.get('text','?'))[:60]; brush_unhandled[key]=brush_unhandled.get(key,0)+1
        for cl in d.get('cliches') or []: cliches_count[cl]=cliches_count.get(cl,0)+1
    ranking='\n'.join('{}. {} — {:.1f}/100 ({} зв.)'.format(i,html.escape(row['manager']),row['avg'],row['n']) for i,row in enumerate(rows,1)) or 'Нет оценённых звонков.'
    top_brush='\n'.join('• «{}…» — {} раз'.format(html.escape(t),n) for t,n in sorted(brush_unhandled.items(),key=lambda x:-x[1])[:5]) or 'Все отговорки отрабатывались.'
    top_cliches='\n'.join('• «{}» — {} раз'.format(html.escape(c),n) for c,n in sorted(cliches_count.items(),key=lambda x:-x[1])[:5]) or 'Штампов не найдено.'
    avg_talk='{:.0f}%'.format(sum(talk_shares)/len(talk_shares)) if talk_shares else 'нет данных'
    text=('<b>Рейтинг менеджеров</b>\n'+ranking+
          '\n\n<b>Встречи назначены:</b> {}/{} ({:.0f}%)'.format(meetings,total,meetings/total*100 if total else 0)+
          '\n<b>Средняя доля речи менеджера:</b> '+avg_talk+
          '\n\n<b>Топ неотработанных отговорок</b>\n'+top_brush+
          '\n\n<b>Топ штампов</b>\n'+top_cliches+
          '\n\nРасходы: '+format(cost,'.0f')+' / '+format(MAX_COST,'.0f')+' ед.')
    await send_chunks(message.bot,message.chat.id,text)

# --------------------------------------------------------- тренажёр возражений
# Сама тренировка живёт в отдельном боте (training_bot.py): в одном боте
# голосовое означало бы две разные вещи — ход тренировки или запись реального
# звонка на разбор. Здесь остаётся только назначение тренировки РОПом.

TRAIN_BOT_USERNAME = os.getenv('TRAIN_BOT_USERNAME', '').strip().lstrip('@')


@router.message(Command('assign_train'))
async def assign_train_command(message: Message):
    """Только РОП. Менеджеру уходит ссылка на бота-тренажёра: одно нажатие и
    запускает того бота (иначе Telegram не даст ему написать первым), и
    стартует назначенную тренировку."""
    if PG_POOL is None:
        return
    actor = await resolve_actor(PG_POOL, message.from_user.id)
    if actor is None or actor.role != 'head':
        await message.answer('Команда доступна только РОПу.')
        return
    args = (message.text or '').split(maxsplit=2)
    if len(args) < 2:
        await message.answer('Использование: /assign_train <добавочный> [тема]')
        return
    extension, topic = args[1], (args[2] if len(args) > 2 else None)
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
        """INSERT INTO pending_train_assignments (client_id, extension, topic, assigned_by_telegram_user_id)
           VALUES ($1,$2,$3,$4) RETURNING id""",
        actor.client_id, extension, topic, actor.telegram_user_id,
    )
    if not TRAIN_BOT_USERNAME:
        await message.answer('Не задан TRAIN_BOT_USERNAME в .env — ссылку на тренажёр не собрать.')
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Начать тренировку',
                              url=f'https://t.me/{TRAIN_BOT_USERNAME}?start=train_{assignment_id}')
    ]])
    topic_line = f' по теме «{html.escape(topic)}»' if topic else ''
    try:
        await message.bot.send_message(manager['telegram_user_id'],
                                        f'РОП назначил вам тренировку{topic_line}. Она пройдёт в боте-тренажёре.',
                                        reply_markup=kb)
    except Exception:
        log.exception('не удалось отправить уведомление о тренировке, доб.=%s', extension)
        await message.answer('Не удалось отправить уведомление менеджеру (возможно, он не запускал бота).')
        return
    await message.answer(f'Назначено, доб. {html.escape(extension)} получит ссылку на тренажёр.')


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
    if actor is None or actor.role != 'head':
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
    if actor is None or actor.role != 'head':
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

def media_from(message):
    media=message.voice or message.audio or message.document
    if not media: return None
    name=getattr(media,'file_name',None) or ('voice.ogg' if message.voice else 'audio.bin')
    extensions={'.mp3','.wav','.ogg','.opus','.m4a','.mp4','.webm','.flac','.aac'}
    if message.document and not(getattr(media,'mime_type','').startswith('audio/') or Path(name).suffix.lower() in extensions): return None
    return media.file_id,int(getattr(media,'file_size',0) or 0),name

@router.message(F.voice|F.audio|F.document)
async def audio_message(message):
    # Голосовое здесь всегда означает запись звонка на разбор: тренировка
    # переехала в отдельного бота, и двусмысленности больше нет.
    media=media_from(message)
    if not media: await message.answer('Документ должен быть аудиофайлом.'); return
    file_id,size,name=media
    if size>MAX_FILE: await message.answer('Файл больше демо-лимита 20 МБ; для часовых записей нужен отдельный upload в хранилище.'); return
    status=await message.answer('Запись поставлена в очередь…')
    manager=(message.caption or 'Не указан').splitlines()[0][:80]
    await queue.put(Job(message.chat.id,status.message_id,file_id,name,manager))

@router.callback_query(F.data.startswith('detail:astra:'))
async def detail_callback(cq: CallbackQuery):
    """«Подробный разбор» для звонков из нового (Mango-через-Claude,
    разбираемых Astra параллельно) конвейера. Права проверяются в момент
    клика по общей таблице employees; результат кешируется в astra_analysis
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
        await cq.answer('Готовлю разбор через Astra, это займёт больше времени, чем у Claude…')
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
        text = pg_render_head(full, manager_name, call['duration_seconds'] or 0, float(call['cost_units'] or 0), '(Astra)',
                               call_time=call_time)
    else:
        text = pg_render_manager(full, call['duration_seconds'] or 0, call_time=call_time)
    await send_chunks(cq.bot, cq.from_user.id, text)


async def main():
    global PG_POOL
    init_db(); bot=Bot(BOT_TOKEN); dp=Dispatcher(); dp.include_router(router)
    PG_POOL = await asyncpg.create_pool(os.environ['DATABASE_URL'], min_size=1, max_size=5)
    tasks=[asyncio.create_task(worker(bot)) for _ in range(int(os.getenv('WORKER_COUNT','2')))]
    try: await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True); await bot.session.close(); await PG_POOL.close()

if __name__=='__main__': asyncio.run(main())
