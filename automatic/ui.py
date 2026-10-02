from __future__ import annotations

import logging
from aiogram import F
from combined import render
from comparison.store import decode
from comparison.registry import sha
from methodology.evaluation import render as render_course
from methodology.messages import split_plain
from methodology.runtime import VERSION
from tools import Forbidden, resolve_actor, get_call
from . import jobs


def install(router,pool_getter):
    @router.callback_query(F.data.startswith('av:'))
    async def callback(cq):
        await cq.answer()
        try:
            if cq.message is None or cq.message.chat.type!='private' or cq.message.chat.id!=cq.from_user.id:
                raise Forbidden('Откройте свои кнопки в личном чате с ботом.')
            bits=(cq.data or '').split(':')
            if len(bits)!=3 or bits[1] not in {'c','b','d'} or not bits[2].isdigit() or len(bits[2])>19:
                raise ValueError('Кнопка устарела или некорректна.')
            pool=pool_getter()
            if pool is None:
                raise ValueError('Бот подключается к базе. Попробуйте позже.')
            actor=await resolve_actor(pool,cq.from_user.id)
            if actor is None or actor.role not in {'manager','head','owner'}:
                raise Forbidden('Доступ к разбору закрыт.')
            call_id=int(bits[2]);call=await get_call(pool,actor,call_id)
            if bits[1]=='c':
                row=await pool.fetchrow('SELECT * FROM methodology_evaluations WHERE call_id=$1 AND client_id=$2 AND version=$3 AND transcript_sha256=$4 ORDER BY id DESC LIMIT 1',call_id,actor.client_id,VERSION,sha(call['transcript'] or ''))
                if row and row['status']=='complete' and row['result']:
                    for chunk in split_plain('📋 Разбор по методике курса\n\n'+render_course(decode(row['result']),detailed=True)):
                        await cq.bot.send_message(cq.from_user.id,chunk,parse_mode=None)
                    return
            else:
                row=await jobs.get(pool,actor,call_id)
                if row and row['status']=='complete' and row['result']:
                    result=decode(row['result'])
                    chunks=render.details(result) if bits[1]=='d' else render.cards(result,decode(row['metadata']))
                    from aiogram.types import InlineKeyboardMarkup,InlineKeyboardButton
                    markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='📋 Критерии и цитаты',callback_data=f'av:d:{call_id}')]]) if bits[1]=='b' else None
                    for index,chunk in enumerate(chunks):
                        prefix='<b>Объединённый разбор</b>\n\n' if index==0 and bits[1]=='b' else ''
                        await cq.bot.send_message(cq.from_user.id,prefix+chunk,parse_mode='HTML',reply_markup=markup if index==len(chunks)-1 else None)
                    return
            if row and row['status'] in {'failed','uncertain'}:
                notice='Разбор не получен из-за ошибки обработки. Неизвестный результат платного запроса автоматически не оплачивается повторно.'
            elif not jobs.enabled():
                notice='Автоматические дополнительные разборы сейчас отключены.'
            elif not row:
                notice='Дополнительного разбора для этого звонка нет. Три варианта автоматически готовятся для новых оценённых звонков после обновления.'
            else:
                notice='Разбор ещё готовится в фоне. Если достигнут дневной бюджет, очередь продолжится после его обновления. Нажмите кнопку позже — повторного платного запроса это не создаёт.'
            await cq.bot.send_message(cq.from_user.id,notice,parse_mode=None)
        except (Forbidden,ValueError) as exc:
            await cq.bot.send_message(cq.from_user.id,str(exc),parse_mode=None)
        except Exception:
            logging.getLogger(__name__).exception('automatic report callback failed')
            await cq.bot.send_message(cq.from_user.id,'Не удалось открыть разбор. Попробуйте кнопку позже.',parse_mode=None)
