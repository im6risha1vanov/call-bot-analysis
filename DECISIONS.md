# DECISIONS

Зафиксированные решения. Не пересматривать без Гриши Иванова.

Recorded decisions. Do not reverse without Гриша Иванов.

## Host and repos

- Live bot lives on **`bot-server`** (`9015421-nt422325`, `/opt/callbot-astra`). **`bt-vps` is another machine** — never patch callbot there.
- Old Claude code in `/opt/callbot` stays on disk but is **not** the running system (services stopped/disabled).
- GitHub copy of Astra, if published, is **private** (`call-bot-analysis`). No secrets in remotes.

## Stack

- **Postgres + Mango only.** SQLite (`callbot.sqlite3`) and manual Telegram upload of recordings were removed (`029e294`). Dual-stack problem is closed.
- Mango is **polled** every 5 minutes (webhooks are paid). Window overlap 3 minutes, dedup by `entry_id`.
- Schedule is **in-process** (`astra_worker` 60s loop), not cron/systemd timers.
- Queue is one process, one task at a time (`FOR UPDATE SKIP LOCKED`, `POLL_INTERVAL_SEC = 5`).

## Agents

- **One tool-using agent** — ROP (`rop_agent.py`). Comment in code: coordinating several agents on these tasks lowers quality.
- Other LLM roles are single-shot prompts (call score/short/review, oversight text, trainer client). Scores and evening-report section 1 are **Python/SQL**, not the model.
- Permissions (`Actor`, `client_id`, `role`, `extension`) are applied in `tools.py` **before** the model. Tool signatures do not take those fields. A manager always sees only themselves.
- Any claim about a named manager must pass `verify_conclusion`; unverified names force a rewrite.

## Product surface

- Trainer is a **separate Telegram bot** so voice in one chat is not overloaded (real-call audio vs trainer role-play).
- Training sessions are scored with the **same** `score_call` / `compute_level` as live calls.
- Delivery: every scored call to head/owner; managers get only `❌`; `level is None` sends nothing.
- ROP evening report: section 1 SQL stats, sections 2–4 from the agent, one paid answer for everyone; personal manager conclusions only through `verify_conclusion`.

## Secrets

- Never commit `.env`, venv, keys, credentials, call recordings, sqlite dumps.
- Mango API keys in DB are Fernet-encrypted with `ENCRYPTION_KEY`.
- Do not paste secrets into chat or embed PATs in git remotes.
