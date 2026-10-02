"""Blind plain-text presentation; complete files, shared quote checks, no truncation."""
from __future__ import annotations
import json
import re
from aiogram.types import BufferedInputFile, InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from tools import resolve_actor
from .registry import LEGACY
from .store import decode, get, authorized

CHOICES={'a':'Лучше А','b':'Лучше Б','equal':'Примерно одинаково','neither':'Оба требуют доработки'}
LABELS={'headline':'Суть звонка','for_head':'Для руководителя','for_manager':'Для менеджера',
'narrative':'Ход разговора','diagnosis':'В чём может быть причина','type':'Тип','reasoning':'Обоснование',
 'turning_point':'Поворотный момент','quote':'Цитата','what_happened':'Что произошло','alternative':'Альтернатива',
 'skill_gaps':'Пробелы в навыках','skill':'Навык','severity':'Важность','evidence':'Основание',
 'recurring_risk':'Риск повторения','lead_quality':'Качество контакта','lead_salvageable':'Можно вернуть контакт',
 'salvage_action':'Действие для возврата','coaching_action':'Задача руководителю','opening':'Общая оценка',
 'did_well':'Что получилось','what':'Действие','why':'Почему','moments':'Разбор моментов','reaction':'Реакция клиента',
 'say_instead':'Сказать вместо','key_mistake':'Главная ошибка','practice':'Упражнение','next_call_focus':'Фокус следующих звонков',
 'result':'Результат','good':'Хорошо','weak':'Слабо','time':'Время','text':'Наблюдение','should_say':'Надо было сказать',
 'metric':'Показатель','value':'Значение','unit':'Единица','period':'Период','channel':'Канал','source':'Источник',
 'assumption':'Предположение','objection':'Возражение','response':'Ответ','client_reaction':'Реакция клиента',
 'unresolved':'Не закрытый вопрос','product':'Продукт','field':'Условие','statement':'Обещание','verdict':'Проверка',
 'reason':'Причина','package':'Пакет','call_center':'Обработка КЦ','replacement_mode':'Замены','tax':'Налоги',
 'record_id':'Запись справочника','action':'Действие','responsible':'Ответственный','deadline':'Срок',
 'scheduled_time':'Время связи','agreed':'Есть согласие','confidence':'Уверенность',
 'roles':'Участники разговора','gatekeeper_present':'В разговоре был секретарь','meeting_booked':'Встреча согласована',
 'meeting_details':'Условия встречи','proposal_sent':'Материалы согласованы','proposal_sent_evidence':'Основание согласия на материалы',
 'decision_maker_contact':'Получен контакт принимающего решение','decision_maker_contact_evidence':'Основание получения контакта',
 'manager_talk_share':'Доля текста менеджера, %','call_cut_short':'Запись короткая или обрывочная',
 'questions':'Вопросы','situational':'Ситуационные','problem':'Проблемные','implication':'Извлекающие','best_question':'Лучший вопрос',
 'brush_offs':'Ранние отговорки','handled':'Отговорка пройдена','how':'Способ ответа','after_pitch':'После презентации',
 'signals':'Сигналы клиента','third_parties':'Упомянутые участники','who':'Участник','followed_up':'Тема развита',
 'problem_agreements':'Признанные затруднения','context':'Контекст','numbers':'Названные числа','about':'О чём число',
 'unanswered_questions':'Вопросы без ответа','cliches':'Речевые штампы','kind':'Вид возражения','unresolved_question':'Оставшийся вопрос'}


def readable(value, depth=0):
    if isinstance(value,dict):
        return '\n'.join('  '*depth+LABELS.get(k,k)+': '+(readable(v,depth+1) if not isinstance(v,(dict,list)) else '\n'+readable(v,depth+1)) for k,v in value.items() if k not in {'methodology_version','catalog_version'})
    if isinstance(value,list):
        return '\n'.join('  '*depth+'• '+readable(v,depth+1) for v in value) or 'не указано'
    if value is None:
        return 'не указано'
    if type(value) is bool:
        return 'да' if value else 'нет'
    return str(value)


def quote_issues(data, transcript):
    normalized=' '.join(transcript.split()); issues=set()
    def walk(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in {'say_instead','should_say','alternative','coaching','practice','next_call_focus'}:
                    continue  # Hypothetical improvements are explicitly not transcript quotes.
                if key=='quote' and isinstance(item,str):
                    q=item.strip().strip('«»"')
                    if q and ' '.join(q.split()) not in normalized:
                        issues.add(q)
                elif key=='evidence' and isinstance(item,str):
                    quoted=re.findall('«([^»]+)»',item)
                    for q in quoted:
                        if ' '.join(q.split()) not in normalized:
                            issues.add(q)
                else:
                    walk(item)
        elif isinstance(value,list):
            for item in value:
                walk(item)
        elif isinstance(value,str):
            for q in re.findall('«([^»]+)»',value):
                if ' '.join(q.split()) not in normalized:
                    issues.add(q)
    walk(data)
    return sorted(issues)


def body(call, label, stages):
    title='Вариант '+label
    if any(stages.get(k,{}).get('status')!='complete' for k in ('score','short','review')):
        states='; '.join(f"{ {'score':'оценка','short':'краткий отчёт','review':'подробный отчёт'}[k]}: {stages.get(k,{}).get('status','нет') }" for k in ('score','short','review'))
        return title+'\n\nПолный разбор не получен. '+states+'\nПричина: '+', '.join(s.get('error') or '' for s in stages.values() if s.get('error'))+'\nУспешные этапы сохранены; неизвестные платные запросы автоматически не повторяются.'
    score=decode(stages['score']['result']); short=decode(stages['short']['result']); review=decode(stages['review']['result'])
    lines=[title,'']
    if 'raw' in score:
        lines+=['КОНТЕКСТ И ОЦЕНКА','Автоматическая классификация продукта, этапа и роли не предусмотрена.',
                f"Балл: {score['score'] if score['score'] is not None else 'нет данных'}/100. Вердикт: {score['level'] or 'нет данных'}.",
                '','КРАТКИЙ ОТЧЁТ',readable(short),'','ПОДРОБНЫЙ РАЗБОР',readable(review),'','КРИТЕРИИ']
        for row in score['rows']:
            status='неприменимо' if not row['applicable'] else 'выполнено' if row['passed'] else 'не выполнено'
            lines.append(f"{row['title']}: {status}; вес {row['weight']}. {row['evidence']}")
        lines+=['','ИЗВЛЕЧЁННЫЕ НАБЛЮДЕНИЯ',readable({k:v for k,v in score['raw'].items() if k!='criteria'}),
                '', 'ОГРАНИЧЕНИЯ','Данные получены из текста, а не повторного прослушивания. Продолжительность реплик по тексту не проверяется. Неуказанные сведения остаются неизвестными.']
        checkdata=[score['raw'],short,review]
    else:
        lines+=['КОНТЕКСТ И ОЦЕНКА','Числовой балл и шкала вердиктов не предусмотрены до экспертной калибровки.',
                '', 'КРАТКИЙ ОТЧЁТ',short['text'].split('\n\n',1)[-1],
                '', 'ПОДРОБНЫЙ РАЗБОР',review['text'].split('\n\n',1)[-1],
                '', 'ПОДТВЕРЖДЁННЫЕ ФАКТЫ',readable(score['facts']),
                '', 'ВОЗРАЖЕНИЯ',readable(score['objections']),
                '', 'КОММЕРЧЕСКИЕ ОБЕЩАНИЯ',readable(score['commercial_claims']),
                '', 'СЛЕДУЮЩЕЕ ДЕЙСТВИЕ',readable(score['next_step']),
                '', 'ОГРАНИЧЕНИЯ',
                f"Уверенность классификации: {score['classification']['confidence']:.0%}. "+score['classification']['reason'],
                'Недостаток данных и неприменимость показаны отдельно в критериях. Неподтверждённые условия не служат основанием штрафа.']
        checkdata=score
    issues=quote_issues(checkdata,call['transcript'])
    lines+=['','ПРОВЕРКА ЦИТАТ']
    lines+=['Следующие цитаты не найдены дословно; не считайте их доказательством:',*['• '+q for q in issues]] if issues else ['В явных полях цитат не найдено неподтверждённых фрагментов. Это не проверка всех выводов модели.']
    return '\n'.join(lines)


def vote_keyboard(run_id, ordinal, call_id):
    rows=[[Button(text=CHOICES[k],callback_data=f'ac:v:{run_id}:{ordinal}:{k}')] for k in CHOICES]
    rows.append([Button(text='Исходная транскрипция',callback_data=f'ac:t:{run_id}:{ordinal}')])
    return Keyboard(inline_keyboard=rows)


async def completion_keyboard(pool, run_id):
    counts=await pool.fetchrow('SELECT (SELECT count(*) FROM analysis_comparison_calls WHERE comparison_id=$1) total,(SELECT count(*) FROM analysis_comparison_votes WHERE comparison_id=$1) voted',run_id)
    if counts['total'] and counts['total']==counts['voted']:
        return Keyboard(inline_keyboard=[[Button(text='Показать версии и итог',callback_data=f'ac:r:{run_id}')]])
    return None


async def send_results(pool,bot,actor,run_id,*,reopen=False):
    run=await get(pool,actor,run_id)
    chat=await bot.get_chat(actor.telegram_user_id)
    if chat.type!='private':
        raise PermissionError('Comparison requires a personal chat')
    async with pool.acquire() as conn:
        lock=f'comparison-deliver:{run_id}:{actor.telegram_user_id}'
        if not await conn.fetchval('SELECT pg_try_advisory_lock(hashtextextended($1,0))',lock):
            return
        try:
            async def send(part,operation):
                # Initial delivery is tracked. Explicit reopening is a requested new copy, free of LLM calls.
                key=f'{actor.telegram_user_id}:{part}'
                if not reopen:
                    state=await conn.fetchval('SELECT status FROM analysis_comparison_delivery WHERE comparison_id=$1 AND part=$2',run_id,key)
                    if state in {'sent','sending','uncertain'}:
                        return
                    await conn.execute("INSERT INTO analysis_comparison_delivery(comparison_id,part,status) VALUES($1,$2,'sending') ON CONFLICT(comparison_id,part) DO UPDATE SET status='sending',error=NULL,updated_at=now()",run_id,key)
                try:
                    msg=await operation()
                except Exception as exc:
                    if not reopen:
                        await conn.execute("UPDATE analysis_comparison_delivery SET status='failed',error=$3,updated_at=now() WHERE comparison_id=$1 AND part=$2",run_id,key,type(exc).__name__)
                    raise RuntimeError('Comparison Telegram delivery failed') from None
                if not reopen:
                    await conn.execute("UPDATE analysis_comparison_delivery SET status='sent',message_id=$3,updated_at=now() WHERE comparison_id=$1 AND part=$2",run_id,key,msg.message_id)
            calls=await conn.fetch('SELECT * FROM analysis_comparison_calls WHERE comparison_id=$1 ORDER BY ordinal',run_id)
            intro=f'Сравнение #{run_id}: {len(calls)} звонка, варианты А и Б. Версии скрыты до вашей оценки.\nОба варианта рассчитаны заново на одной транскрипции. Баллы разных шкал напрямую не сравнивайте. Полные разборы — в текстовых файлах.'
            if len({decode(c['metadata'])['extension'] for c in calls})<2:
                intro+='\nДоступные звонки принадлежат одному менеджеру.'
            await send('intro',lambda:bot.send_message(actor.telegram_user_id,intro,parse_mode=None))
            for call in calls:
                m=decode(call['metadata']); duration=m['duration_seconds'] or 0
                card=f"Звонок {call['ordinal']} из {len(calls)} · ID {call['call_id']}\n{m['manager']}\n{m['call_started_at']} ({m['timezone']}) · {duration//60}:{duration%60:02d}\nКлиент: {m['phone']}"
                await send(f"{call['ordinal']}:card",lambda:bot.send_message(actor.telegram_user_id,card,parse_mode=None))
                for label,version in [('А',call['a_version']),('Б',call['b_version'])]:
                    rows=await conn.fetch('SELECT * FROM analysis_comparison_stages WHERE comparison_id=$1 AND ordinal=$2 AND version=$3',run_id,call['ordinal'],version)
                    stages={r['stage']:dict(r) for r in rows}
                    text=body(call,label,stages)
                    document=BufferedInputFile(text.encode('utf-8'),filename=f"comparison_{run_id}_call_{call['ordinal']}_{label}.txt")
                    await send(f"{call['ordinal']}:{label}",lambda:bot.send_document(actor.telegram_user_id,document,caption=f"Звонок {call['ordinal']} · Вариант {label} · полный разбор",parse_mode=None))
                kb=vote_keyboard(run_id,call['ordinal'],call['call_id']) if actor.telegram_user_id==run['initiator'] and not run['revealed_at'] else None
                await send(f"{call['ordinal']}:vote",lambda:bot.send_message(actor.telegram_user_id,f"Оцените полезность и точность вариантов для звонка {call['ordinal']}. Оценку можно изменить до раскрытия.",reply_markup=kb,parse_mode=None))
        finally:
            await conn.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))',lock)


async def summary(pool, actor, run_id):
    run=await get(pool,actor,run_id,initiator=True)
    if not run['revealed_at']:
        raise ValueError('Версии ещё не раскрыты.')
    calls=await pool.fetch('SELECT c.*,v.choice,v.comment FROM analysis_comparison_calls c JOIN analysis_comparison_votes v USING(comparison_id,ordinal) WHERE comparison_id=$1 ORDER BY ordinal',run_id)
    lines=[f'Итог сравнения #{run_id}']
    for call in calls:
        lines.append(f"\nЗвонок {call['ordinal']}: А — {call['a_version']}; Б — {call['b_version']}.\nВаша оценка: {CHOICES[call['choice']]}.\nКомментарий: {call['comment'] or 'не оставлен'}")
    costs=await pool.fetch('SELECT version,sum(cost_units) cost FROM analysis_comparison_stages WHERE comparison_id=$1 GROUP BY version ORDER BY version',run_id)
    for row in costs:
        lines.append(f"{row['version']}: {float(row['cost']):,.0f} кредитных единиц (расчёт по usage, не доллары).")
    if len(costs)==2:
        lines.append(f"Разница расходов: {abs(float(costs[0]['cost'])-float(costs[1]['cost'])):,.0f} единиц.")
    unknown=await pool.fetchval("SELECT count(*) FROM analysis_comparison_stages WHERE comparison_id=$1 AND status='uncertain' AND usage IS NULL",run_id)
    if unknown:
        lines.append(f'Запросов с неизвестным биллингом: {unknown}; полный расход не подтверждён.')
    lines.append('Это предварительное пользовательское сравнение на небольшой выборке, а не надёжная статистическая оценка. Победитель автоматически не выбирается. Производственная методика не изменена.')
    return '\n'.join(lines)
