# PROJECT_CONTEXT

Владелец: Гриша Иванов. Deliverable — существующий Telegram-бот в `/opt/callbot-astra` на `9015421-nt422325.twc1.net`. Не скаффолдить новый сайт или бота.

Owner: Гриша Иванов. The product is the live bot on host `9015421-nt422325`, directory `/opt/callbot-astra`. Do not scaffold a new website or bot.

## Hosts

- Place server work on self-hosted worker **`bot-server`** (`0cf47cd1-68ef-4ca8-94cb-b498ed8b05e9`).
- **`bt-vps` is a different host** (`ams-1-vm-cvf5`, `bt-dispatch-bot`) — do not patch callbot there.
- Keep `agent worker start --name bot-server` in tmux.
- Do not paste API keys or `.env` into chat. Do not ask for account passwords.
- Bot copy is Russian.

## Goal

Разбирать исходящие холодные звонки отдела продаж (Mango) и отдавать оценки, короткие отчёты, дайджесты руководителю и голосовой тренажёр менеджерам.

Score outbound cold calls from Mango, deliver reports to managers/heads/owner, and run a separate voice trainer.

## Architecture

**4 процесса** (systemd) + Postgres `callbot`. Настоящий агент с инструментами **ровно один** — агент РОПа (`rop_agent.py`). Остальные LLM-роли — одноходовые промты без tool-use.

| Unit | Starts | Role |
|---|---|---|
| `callbot-astra.service` | `demo_bot.py` | main Telegram bot `@codex_bot_example_bot` «Callbot Averon» (id 8738887420) |
| `callbot-astra-worker.service` | `astra_worker.py` | poll Mango every 5 min, employee sync, enqueue digest/oversight tasks |
| `callbot-astra-queue-runner.service` | `run_queue_runner.py` → `queue_runner.py` | one-at-a-time task runner |
| `callbot-astra-trainer.service` | `training_bot.py` | trainer `@averon_training_bot` «Averon Training» (id 8985502833) |

All four: `User=callbot-astra`, `EnvironmentFile=/opt/callbot-astra/.env`, venv Python 3.14, `Restart=always`. No systemd timers / crontab — schedule lives in `astra_worker.main()` (60s loop).

Call path: Mango poll (`stats/calls/request`, 300s, overlap 3 min, dedup `entry_id`) → `calls` + task `analyze_call` → `handlers/analyze_call.py` (budget → recording → Deepgram → `score_call` / `short_report_call`) → deliver. Heads/owner get every scored call; manager gets only `❌`.

ROP tools (rights resolved in `tools.py` **before** the model): `get_stats`, `find_calls`, `get_call`, `compare_periods`, `get_criteria_breakdown`, `get_successful_evidence`, `get_lead_diagnosis_signal`, `verify_conclusion`, `get_training_history`.

Postgres tables include `clients`, `employees`, `calls`, `astra_analysis`, `astra_daily_spend`, `astra_digest_state`, `tasks`, `rop_digest_state`, `rop_agent_daily_spend`, `oversight_state`, `limit_hits`, `training_sessions`, `pending_train_assignments`, `invite_tokens`, `daily_spend`. Mango keys stored encrypted (`Fernet` / `ENCRYPTION_KEY`).

## Status (snapshot 18.09.2026, git `029e294`)

- Old Claude stack **off**: `callbot.service`, `callbot-worker.service`, `callbot-api.service` stopped/disabled. Files remain on disk under `/opt/callbot` and must not be used as the live bot.
- SQLite / manual upload **removed** from Astra (`029e294`). Postgres + Mango only.
- Astra still running: bot, worker, queue-runner, trainer.
- Local git was `master` without a remote; this repo is the backup other agents should clone.
- Spec completed through **stage 5** (queue → tools → ROP agent → trainer → oversight). **Stage 6 is not specified.**

## Remaining (see CURRENT_TASK.md)

- `/approvals` idle
- Evening / weekly / monthly digests never ran
- No stage 6

## Do not

- Work on `bt-vps` / `ams-1-vm-cvf5` / `bt-dispatch-bot`
- Push or patch `/opt/callbot` as if it were production
- Commit `.env`, venv, keys, credentials, call recordings, sqlite dumps
- Print `.env` values
