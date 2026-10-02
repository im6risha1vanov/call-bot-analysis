"""Readable Telegram cards and optional full criteria; every chunk is valid HTML."""
from __future__ import annotations
import html
from datetime import datetime
from comparison.packages.course.methodology.profiles import ROLE_LABELS,PRODUCT_LABELS,STAGE_LABELS
from methodology.messages import split_plain
from aiogram.types import InlineKeyboardButton as Button,InlineKeyboardMarkup as Keyboard

STATUS={'passed':'✅ выполнено','failed':'⚠️ не выполнено','not_applicable':'— неприменимо','insufficient_data':'❔ недостаточно данных'}
CAUSE={'manager':'Действия менеджера','script':'Сценарий разговора','base':'Соответствие контакта предложению','insufficient_data':'Причина пока не установлена'}


def esc(value):
    return html.escape(str(value or ''))


def units(text):
    return len(text.encode('utf-16-le'))//2


def pack(blocks,limit=3800):
    """Split complete sections, not tags; exceptionally long sections become escaped plain text."""
    chunks=[];current=''
    for block in blocks:
        if units(block)>limit:
            # Each block is generated from fixed markup. Preserve all its content if quotes are long.
            import re
            plain=html.unescape(re.sub(r'</?(?:b|i)>','',block))
            pieces=[esc(part) for part in split_plain(plain,limit=500)]
        else:
            pieces=[block]
        for index,piece in enumerate(pieces):
            separator='\n\n' if current and index==0 else ''
            if current and units(current+separator+piece)>limit:
                chunks.append(current);current='';separator=''
            current=current+separator+piece
    if current:chunks.append(current)
    return chunks


def cards(result,metadata):
    r=result['readable_report'];c=result['classification'];counts=result['quality']['counts']
    duration=int(metadata.get('duration_seconds') or 0)
    try: when=datetime.fromisoformat(metadata['call_started_at']).strftime('%d.%m.%Y %H:%M')
    except (KeyError,ValueError,TypeError):when='время неизвестно'
    blocks=[f"<b>📞 {esc(metadata['manager'])} · {duration//60}:{duration%60:02d}</b>\n{esc(when)} · {esc(metadata['timezone'])} · звонок №{metadata['call_id']}\n{esc(metadata['phone'])}",
            '<b>🎯 Итог звонка</b>\n'+esc(r['summary'] or result['outcome']['description'] or 'Итог не установлен'),
            esc(PRODUCT_LABELS[c['product']])+' · '+esc(STAGE_LABELS[c['stage']])+'\n'+esc(ROLE_LABELS[c['sales_role']])]
    if not result['outcome']['confirmed']:
        blocks.append('Конкретный следующий шаг с согласием клиента не подтверждён.')
    if r['strengths']:
        good=['<b>✅ Что получилось</b>']
        for item in r['strengths']:
            good.extend(['• '+esc(item['what']), '«'+esc(item['quote'])+'»',esc(item['why'])])
        blocks.append('\n'.join(filter(None,good)))
    else:
        blocks.append('<b>✅ Что получилось</b>\nВ записи недостаточно подтверждений для отдельного сильного действия.')
    if r['main_miss']:
        miss=r['main_miss']
        evidence='' if any(m['quote']==miss['quote'] for m in r['moments']) else '\n«'+esc(miss['quote'])+'»'
        blocks.append('<b>📌 Главный упущенный момент</b>\n'+esc(miss['explanation'])+evidence)
    for index,item in enumerate(r['moments'],1):
        lines=[f"<b>💬 Что изменить{' · '+str(index) if len(r['moments'])>1 else ''}</b>",esc(item['what']),
               'Менеджер: «'+esc(item['quote'])+'»']
        if item['reaction_quote']:lines.append('Клиент: «'+esc(item['reaction_quote'])+'»')
        lines.append(esc(item['why']))
        if item['say_instead']:lines.append('<b>Можно сказать:</b> «'+esc(item['say_instead'])+'»')
        blocks.append('\n'.join(filter(None,lines)))
    if not r['moments'] and not r['main_miss']:
        blocks.append('Главный недочёт не установлен по подтверждённым наблюдениям; это не доказательство отсутствия ошибок.')
    action=[]
    if r['next_focus']:action.append('<b>На следующий разговор:</b> '+esc(r['next_focus']))
    if r['exercise']:action.append('<b>Короткая практика:</b> '+esc(r['exercise']))
    if action:blocks.append('<b>🎯 Что сделать дальше</b>\n'+'\n'.join(action))
    leader=r['leader']
    blocks.append('<b>👤 Руководителю</b>\n'+esc(CAUSE[leader['cause']])+': '+esc(leader['reason'])
                  + ('\n'+esc(leader['action']) if leader['action'] else ''))
    notes=list(r['limits'])+r['validation_warnings']
    if not result['narrow_profile_applied']:notes.append('Продукт или этап определён неуверенно: оценены только общие действия.')
    if result['honesty']['unverified_claims']:notes.append('Коммерческие обещания требуют проверки по утверждённым условиям.')
    if notes:blocks.append('<b>ℹ️ Границы оценки</b>\n'+'\n'.join('• '+esc(n) for n in dict.fromkeys(notes)))
    blocks.append(f"<i>Подтверждено действий: {counts['passed']}; недочётов: {counts['failed']}. Подробные критерии — по кнопке.</i>")
    return pack(blocks)


def details(result):
    blocks=['<b>📋 Критерии и основания</b>']
    for row in result['rows']:
        block=f"<b>{esc(row['title'])}</b>\n{STATUS[row['status']]}"
        if row['evidence']:block+='\n«'+esc(row['evidence'])+'»'
        if row['reason']:block+='\n'+esc(row['reason'])
        blocks.append(block)
    blocks.append('<i>Числовой балл не назначен: веса объединённой методики требуют калибровки. Качество разговора и коммерческий исход учитываются отдельно.</i>')
    return pack(blocks)


def keyboard(comparison_id,ordinal):
    return Keyboard(inline_keyboard=[[Button(text='📋 Критерии и цитаты',callback_data=f'cb:d:{comparison_id}:{ordinal}')],
                                     [Button(text='Исходная транскрипция',callback_data=f'ac:t:{comparison_id}:{ordinal}')]])
