from __future__ import annotations
import logging
from aiogram import F
from aiogram.filters import Command
from tools import resolve_actor,Forbidden
from comparison.store import decode
from . import jobs,render
MENU='Объединённый разбор'


def install(router,pool_getter):
    @router.message(Command('combined_analysis'))
    @router.message(F.text==MENU)
    async def command(message):
        pool=pool_getter()
        if pool is None:return
        actor=await resolve_actor(pool,message.from_user.id)
        try:
            if message.chat.type!='private':raise Forbidden('Откройте личный чат бота: /combined_analysis.')
            args=(message.text or '').split()[1:] if (message.text or '').startswith('/') else []
            if args and (len(args)!=1 or not args[0].isdigit()):
                raise ValueError('/combined_analysis [номер сравнения] — пробный объединённый разбор выбранных звонков.')
            comparison_id,created=await jobs.request(pool,actor,int(args[0]) if args else None)
            states=await pool.fetch('SELECT status FROM combined_reports WHERE comparison_id=$1 ORDER BY ordinal',comparison_id)
            if any(r['status'] in {'pending','processing','received'} for r in states):
                await message.answer(f'Объединённый разбор сравнения #{comparison_id} '+('запущен' if created else 'уже готовится')+'. Результаты придут сюда. Повторный запрос не создаёт дополнительной оплаты.')
            else:
                await jobs.deliver(pool,message.bot,actor,comparison_id,reopen=True)
        except (Forbidden,ValueError) as exc:
            await message.answer(str(exc),parse_mode=None)
        except Exception:
            logging.getLogger(__name__).exception('combined command failed')
            await message.answer('Не удалось открыть разбор. Результат сохранён; повторите /combined_analysis.')

    @router.callback_query(F.data.startswith('cb:'))
    async def callback(cq):
        await cq.answer()
        pool=pool_getter()
        if pool is None:return
        try:
            if cq.message is None or cq.message.chat.type!='private' or cq.message.chat.id!=cq.from_user.id:
                raise Forbidden('Используйте свои кнопки в личном чате.')
            bits=(cq.data or '').split(':')
            if len(bits)!=4 or bits[:2]!=['cb','d'] or not all(b.isdigit() for b in bits[2:]):
                raise ValueError('Кнопка устарела или некорректна.')
            actor=await resolve_actor(pool,cq.from_user.id)
            row=await jobs.get(pool,actor,int(bits[2]),int(bits[3]))
            if row['status']!='complete':raise ValueError('Подробности пока не готовы.')
            for chunk in render.details(decode(row['result'])):
                await cq.bot.send_message(cq.from_user.id,chunk,parse_mode='HTML')
        except (Forbidden,ValueError) as exc:
            await cq.bot.send_message(cq.from_user.id,str(exc),parse_mode=None)
        except Exception:
            logging.getLogger(__name__).exception('combined callback failed')
            await cq.bot.send_message(cq.from_user.id,'Не удалось открыть критерии. Повторите /combined_analysis.',parse_mode=None)
