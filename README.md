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
