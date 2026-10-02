# Callbot Averon — анализ звонков / call analysis

Telegram-бот разбора холодных звонков для Гриши Иванова. Живой код — `/opt/callbot-astra` на хосте `9015421-nt422325` (self-hosted worker `bot-server`). Это не новый проект и не сайт.

English: production Telegram bot that scores outbound sales calls (Mango recordings → Deepgram → Astra/Claude). Four systemd processes, Postgres only. One tool-using agent (ROP).

## Docs for other agents

Read these first, in order:

1. [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) — goal, host, status
2. [CURRENT_TASK.md](CURRENT_TASK.md) — what is still open
3. [DECISIONS.md](DECISIONS.md) — why the architecture looks like this
4. [AI_INSTRUCTIONS.md](AI_INSTRUCTIONS.md) — sync and safety rules

## Architecture (short)

Four processes + Postgres `callbot`. Old Claude stack `/opt/callbot` is stopped/disabled. SQLite and manual call upload are gone (HEAD around `029e294`).

| Unit | Entry | Role |
|---|---|---|
| `callbot-astra.service` | `demo_bot.py` | main bot `@codex_bot_example_bot` «Callbot Averon» |
| `callbot-astra-worker.service` | `astra_worker.py` | Mango poll every 5 min, employee sync, digest schedule |
| `callbot-astra-queue-runner.service` | `run_queue_runner.py` | task queue (`analyze_call`, digests, oversight) |
| `callbot-astra-trainer.service` | `training_bot.py` | trainer bot `@averon_training_bot` |

LLM roles: call analyst (`analysis.py`, `gpt-6-astra` via `ai.starimg.ru`), **one** ROP tool-agent (`rop_agent.py` + `tools.py`, Claude), conclusion verifier, daily oversight prompt, trainer client simulator. ASR: Deepgram. Trainer TTS: Yandex SpeechKit.

## Status (as of 18.09.2026)

- Astra units running on `bot-server`.
- Old services `callbot.service`, `callbot-worker.service`, `callbot-api.service` stopped and disabled.
- `/approvals` UI exists but is idle (nothing raises `NeedsApproval`).
- Evening / weekly / monthly ROP digests have never run. Morning digest and oversight have.
- Multi-agent spec is at stage 5. Stage 6 is not specified.

## Local / server run

Production: systemd, `User=callbot-astra`, `EnvironmentFile=/opt/callbot-astra/.env`, venv Python 3.14.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# copy secrets into .env on the server only — never commit it
.venv/bin/python demo_bot.py
.venv/bin/python astra_worker.py
.venv/bin/python run_queue_runner.py
.venv/bin/python training_bot.py
```

Required env **names** (values live only in server `.env`): `BOT_TOKEN`, `TRAIN_BOT_TOKEN`, `TRAIN_BOT_USERNAME`, `DEEPGRAM_API_KEY`, `CVC_API_KEY`, `CVC_BASE_URL`, `CVC_MODEL`, `ANTHROPIC_API_KEY`, `DATABASE_URL`, `ENCRYPTION_KEY`, plus optional TTS (`TTS_PROVIDER`, `YANDEX_TTS_API_KEY`, `YANDEX_TTS_VOICE`) and spend knobs.

Do not commit `.env`, venv, keys, credentials, call recordings, or sqlite dumps.

## Constraints

- Work on `bot-server` / `/opt/callbot-astra`. Do **not** patch `bt-vps` (`ams-1-vm-cvf5`, `bt-dispatch-bot`) or `/opt/callbot`.
- Do not paste API keys or `.env` into chat.
- Owner: Гриша Иванов.

## Методика курса — 1 октября 2026

Ветка `feat/course-methodology-2026-10` добавляет параллельную оценку по ролям,
продуктам и этапам, предметный тренажёр и полный откат одной командой.
Исходная шкала и её автоматические решения сохранены. Перед дальнейшей работой
прочитайте [METHODOLOGY_IMPLEMENTATION.md](METHODOLOGY_IMPLEMENTATION.md): там
режимы, команды, таблицы, проверки, ограничения и контекст следующему сеансу.

## Меню тренажёра — 2 октября 2026

В личном чате [@averon_training_bot](https://t.me/averon_training_bot) отправьте
`/start` или `/help`: под полем ввода появится постоянное меню по две кнопки
в строке: «🎯 Начать тренировку», «📋 Ситуации», «⚡ Короткая отработка»,
«🔁 Повторить», «⏹ Завершить», «❓ Помощь».

«Начать тренировку» и «Ситуации» открывают полный разговор; «Короткая отработка»
сразу запускает пять разных ситуаций в случайном порядке, без каталога.
Ситуации берутся из актуального реестра без повторов внутри серии и сохраняются
в БД при запуске. Каждое упражнение разбирается по своему продукту, роли и этапу;
цитаты сверяются с ответом именно на это упражнение. Повтор короткой серии
сохраняет её ситуации и меняет обстоятельства. Прежняя команда
`/train возражения <ситуация>` доступна для целевой отработки и назначений.
Каталог полного разговора строится из актуального реестра: до восьми ситуаций
на странице, по две кнопки в строке. Выбор сразу запускает сессию; стрелки
редактируют то же сообщение. «Общая тренировка» использует существующий подбор
сценария. «Повторить» меняет обстоятельства последней **завершённой** тренировки.
Активная сессия сохраняется при повторном запуске, старые кнопки завершения
проверяют номер сессии. Голос, текст, прежние команды, назначенные тренировки,
лимиты и разборы работают через прежние функции. Промты и шкала не изменены.

Код интерфейса: `training_menu.py`, обработчики: `training_bot.py`.
Проверки меню: `tests/test_training_menu.py`; выполняются с подменёнными
Telegram, TTS, Deepgram и запуском тренировок, без производственных запросов.
При выключенной методике доступны общие тренировки обоих режимов.

По запросу владельца 2 октября 2026 снят дневной лимит количества тренировок:
полный разговор, короткая отработка и повтор доступны без ограничения числа
сессий в день. Сохраняются одна активная сессия, права доступа, защита от
повторных кликов, ограничения длительности, числа реплик и бюджета одной сессии.

В короткой отработке голосом звучит только возражение клиента. Название и номер
упражнения, обстоятельства, этап и пояснения остаются текстом. Это действует
с первого упражнения; текстовые итоги и обратная связь не озвучиваются.
При недоступном TTS упражнение остаётся доступным в тексте без дублирования.
Контекст показан русскими подписями и короткими пунктами («Предложение»,
«Оплата», «Источники», «Собеседник»), с разделением на абзацы и репликой клиента
отдельно. Служебные ключи и JSON пользователю не показываются.
Досрочно завершённая короткая серия разбирается по фактически сохранённым
ответам. Лишние оценки неотвеченных упражнений из ответа модели исключаются;
проверка цитат, критериев и полноты разбора полученных ответов сохраняется.
При повторной обработке используется сохранённый ответ модели без нового
платного запроса.


## Слепое сравнение методик анализа

В личном чате основного бота руководитель/владелец открывает `/compare_analysis` или кнопку «Сравнить методики» после `/start` и `/help`. Берутся два последних пригодных разобранных звонка, по возможности разных менеджеров. `/compare_analysis <ID>` открывает сохранённый результат; `/compare_analysis new` явно начинает новый эксперимент. Повторный запрос во время работы не создаёт дополнительных расходов.

Обе методики заново работают с одним сохранённым текстом и общими настройками модели. Пакеты оценки, краткого и подробного отчётов закреплены с хешами в `comparison/packages/manifest.json`: точная исходная версия из коммита `52e4d575`, новая — из `3fd96b5`. Новая методика использует собственные детерминированные шаблоны отчётов и пока не имеет калиброванного числового балла; баллы разных шкал напрямую не сравниваются. Производственные результаты, переключатель методики, рейтинги и сводки не меняются.

Полные варианты А/Б приходят отдельными UTF-8 текстовыми файлами с одинаковым оформлением и без обрезания. Назначение букв случайно для каждого звонка и сохраняется. Кнопки позволяют оценить каждый звонок, дополнить выбор текстовым комментарием без вызова LLM и изменить его до раскрытия. После всех оценок кнопка «Показать версии и итог» раскрывает версии и расходы в кредитных единицах. Это небольшое пользовательское сравнение, а не статистическое определение победителя.

Отдельные таблицы `analysis_comparison*` создаются идемпотентной миграцией `migrations/002_analysis_comparison.sql`; задания `compare_analysis` выполняет отдельный цикл существующего queue-runner. Каждый оплаченный ответ сохраняется до проверки JSON. После сбоя готовые этапы используются повторно, недостающие безопасные этапы продолжаются, а запросы с неизвестным результатом автоматически не оплачиваются второй раз. Результаты доставки учитываются отдельно; при неопределённой доставке результат можно явно переоткрыть командой. Авторизация повторно проверяется перед анализом, просмотром, оценкой и отправкой; голосование и раскрытие доступны только инициатору.
