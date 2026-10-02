from __future__ import annotations
from aiogram import F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from tools import Forbidden,resolve_actor,get_call
from .store import create,get,vote,reveal,decode
from .presentation import send_results,completion_keyboard,summary

MENU='Сравнить методики'


def install(router,pool_getter):
    @router.message(Command('compare_analysis'))
    @router.message(F.text==MENU)
    async def command(message):
        pool=pool_getter()
        if pool is None:
            return
        actor=await resolve_actor(pool,message.from_user.id)
        if message.chat.type!='private':
            await message.answer('Откройте личный чат с ботом и /compare_analysis.')
            return
        args=(message.text or '').split()[1:] if (message.text or '').startswith('/') else []
        try:
            if args and args[0].isdigit() and len(args)==1:
                run_id=int(args[0]); await get(pool,actor,run_id)
                created=False
            elif not args or args==['new']:
                run_id,created=await create(pool,actor,new=args==['new'])
            else:
                await message.answer('/compare_analysis — текущее сравнение; /compare_analysis <ID> — открыть; /compare_analysis new — явно начать новое.')
                return
            experiment=await get(pool,actor,run_id)
            if experiment['status'] in {'queued','running'}:
                complete=await pool.fetchval("SELECT count(*) FROM analysis_comparison_stages WHERE comparison_id=$1 AND status='complete'",run_id)
                await message.answer(f"Сравнение #{run_id}: {'запущено' if created else 'уже готовится'}, завершено этапов: {complete}. Результаты придут сюда; повторный запрос не создаёт платных дублей.",parse_mode=None)
            else:
                await send_results(pool,message.bot,actor,run_id,reopen=True)
                kb=await completion_keyboard(pool,run_id) if actor.telegram_user_id==experiment['initiator'] else None
                await message.answer(f'Сохранённое сравнение #{run_id}. Для нового: /compare_analysis new.',reply_markup=kb,parse_mode=None)
                if experiment['revealed_at'] and actor.telegram_user_id==experiment['initiator']:
                    await message.answer(await summary(pool,actor,run_id),parse_mode=None)
        except (Forbidden,ValueError,PermissionError) as exc:
            await message.answer(str(exc),parse_mode=None)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('comparison command failed')
            await message.answer('Не удалось открыть сравнение. Результаты сохраняются; повторите /compare_analysis.',parse_mode=None)

    @router.callback_query(F.data.startswith('ac:'))
    async def callback(cq):
        # Answer before DB/network work, including malformed, unauthorized, and failed actions.
        await cq.answer()
        pool=pool_getter()
        if pool is None:
            return
        actor=await resolve_actor(pool,cq.from_user.id)
        try:
            if cq.message is None or cq.message.chat.type!='private' or cq.message.chat.id!=cq.from_user.id:
                raise Forbidden('Используйте свои кнопки в личном чате.')
            bits=(cq.data or '').split(':')
            if len(bits)<3 or not bits[2].isdigit():
                raise ValueError('Устаревшая или некорректная кнопка.')
            action=bits[1]; run_id=int(bits[2])
            experiment=await get(pool,actor,run_id,initiator=action in {'v','r','s'})
            if action=='v' and len(bits)==5 and bits[3].isdigit():
                ordinal=int(bits[3]); await vote(pool,actor,run_id,ordinal,bits[4])
                kb=await completion_keyboard(pool,run_id)
                rows=[[Button(text='Без комментария',callback_data=f'ac:s:{run_id}')]]
                if kb:
                    rows+=kb.inline_keyboard
                await cq.bot.send_message(cq.from_user.id,'Оценка сохранена. При желании отправьте следующим сообщением комментарий; он будет сохранён без обращения к модели. Или нажмите «Без комментария». До раскрытия можно изменить оценку теми же кнопками.',reply_markup=Keyboard(inline_keyboard=rows),parse_mode=None)
            elif action=='s' and len(bits)==3:
                await pool.execute('UPDATE analysis_comparisons SET pending_comment_ordinal=NULL WHERE id=$1',run_id)
                await cq.bot.send_message(cq.from_user.id,'Комментарий пропущен.',reply_markup=await completion_keyboard(pool,run_id),parse_mode=None)
            elif action=='r' and len(bits)==3:
                await reveal(pool,actor,run_id)
                await cq.bot.send_message(cq.from_user.id,await summary(pool,actor,run_id),parse_mode=None)
            elif action=='t' and len(bits)==4 and bits[3].isdigit():
                call=await pool.fetchrow('SELECT * FROM analysis_comparison_calls WHERE comparison_id=$1 AND ordinal=$2',run_id,int(bits[3]))
                if not call:
                    raise ValueError('Звонок не найден.')
                # Reuse production transcript permissions; send the pinned experimental snapshot.
                await get_call(pool,actor,call['call_id'])
                await cq.bot.send_document(cq.from_user.id,BufferedInputFile(call['transcript'].encode('utf-8'),filename=f"call_{call['call_id']}_transcript.txt"),caption='Исходная транскрипция выбранного звонка (снимок сравнения).',parse_mode=None)
            else:
                raise ValueError('Устаревшая или некорректная кнопка.')
        except (Forbidden,ValueError,PermissionError) as exc:
            await cq.bot.send_message(cq.from_user.id,str(exc),parse_mode=None)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('comparison callback failed')
            await cq.bot.send_message(cq.from_user.id,'Не удалось обработать действие. Сравнение сохранено: /compare_analysis.',parse_mode=None)
