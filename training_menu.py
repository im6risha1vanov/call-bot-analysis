"""Telegram presentation only; scenarios and training rules live in their existing modules."""
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup)

from methodology import scenarios

START = '🎯 Начать тренировку'
SCENARIOS = '📋 Ситуации'
DRILL = '⚡ Короткая отработка'
REPEAT = '🔁 Повторить'
STOP = '⏹ Завершить'
HELP = '❓ Помощь'
ACTIONS = (START, SCENARIOS, DRILL, REPEAT, STOP, HELP)
MODES = {'d': 'dialog', 'o': 'drill'}
MODE_NAMES = {'dialog': 'Разговор целиком', 'drill': 'Короткая отработка'}
PAGE_SIZE = 8


def main_keyboard():
    options = {'resize_keyboard': True}
    if 'is_persistent' in ReplyKeyboardMarkup.model_fields:
        options['is_persistent'] = True
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=ACTIONS[n]), KeyboardButton(text=ACTIONS[n + 1])]
        for n in range(0, len(ACTIONS), 2)
    ], **options)


def button(text, data):
    if len(data.encode('utf-8')) > 64:
        raise ValueError('Training callback exceeds Telegram limit')
    return InlineKeyboardButton(text=text, callback_data=data)


def mode_code(mode):
    return next(code for code, value in MODES.items() if value == mode)


def catalog(mode, page=0, *, enabled=True):
    """Read the current registry every time. Both course modes support every scenario."""
    code = mode_code(mode)
    items = scenarios.SCENARIOS if enabled else []
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    visible = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = []
    for n in range(0, len(visible), 2):
        rows.append([button(s['title'] if len(s['title']) <= 27 else s['title'][:26] + '…',
                            f"tm:r:{code}:{s['id']}") for s in visible[n:n + 2]])
    navigation = []
    if page:
        navigation.append(button('⬅️ Назад', f'tm:p:{code}:{page - 1}'))
    if page + 1 < pages:
        navigation.append(button('Вперёд ➡️', f'tm:p:{code}:{page + 1}'))
    if navigation:
        rows.append(navigation)
    rows.append([button('Общая тренировка', f'tm:g:{code}'), button('🏠 Меню', 'tm:h')])
    text = f"{MODE_NAMES[mode]}. Выберите ситуацию — она запустится сразу.\nСтраница {page + 1}/{pages}."
    if visible:
        text += '\n\n' + '\n\n'.join(f"{s['title']}\n{s['goal']}" for s in visible)
    else:
        text += '\nСценарии по курсу сейчас выключены. Доступна общая тренировка.'
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def parse(data):
    """Strict callback allowlist; stale/unknown scenarios are never fuzzy-matched."""
    if len((data or '').encode('utf-8')) > 64:
        return None
    parts = (data or '').split(':')
    if len(parts) == 2 and parts[0] == 'tm' and parts[1] in {'h', 'c'}:
        return parts[1], None, None
    if len(parts) == 3 and parts[:2] == ['tm', 's'] and parts[2].isascii() and parts[2].isdigit() and int(parts[2]) > 0:
        return 's', None, int(parts[2])
    if len(parts) < 3 or parts[0] != 'tm' or parts[2] not in MODES:
        return None
    action, mode = parts[1], MODES[parts[2]]
    if action == 'g' and len(parts) == 3:
        return action, mode, None
    if action == 'p' and len(parts) == 4 and parts[3].isascii() and parts[3].isdigit() and len(parts[3]) <= 6:
        return action, mode, int(parts[3])
    if action == 'r' and len(parts) == 4 and any(s['id'] == parts[3] for s in scenarios.SCENARIOS):
        return action, mode, parts[3]
    return None


def active_keyboard(session_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        button('Продолжить', 'tm:c'), button(STOP, f'tm:s:{session_id}')
    ]])
