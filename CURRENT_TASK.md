# CURRENT_TASK

Открытые задачи по живому боту (источник: состояние на 18.09.2026). Не выдумывать этап 6.

Open work on the live bot (snapshot 18.09.2026). Do not invent stage 6.

## Blocked: GitHub publish

Push of this tree to a new private GitHub repo `call-bot-analysis` did **not** finish. On `bot-server` (`9015421-nt422325`) `gh` is installed but **not logged in**. There is no GitHub SSH key and no `GITHUB_TOKEN`/`GH_TOKEN`. Needs Гриша: `gh auth login` on this host as the GitHub user that should own the private repo (then push local `master` to `main`).

## Product remaining

1. **`/approvals` is idle.** `NeedsApproval` is declared in `queue_runner.py`, the runner and Telegram UI (`approve:` / `reject:`) exist, but **no handler raises it**. There have been zero `awaiting_approval` tasks.
2. **Evening / weekly / monthly ROP digests have never executed.** Task types exist (`rop_digest_evening`, `rop_digest_weekly`, `rop_digest_monthly`). As of the last production slice, `tasks` had `analyze_call|done|47`, `oversight_report|done|2`, `rop_digest_morning|done|3` only. Personal manager digests and morning ROP digest are the ones that actually ran.
3. **No stage 6.** Multi-agent work is specified through stage 5 (queue → tools → ROP agent → trainer → oversight). There is no stage 6 spec — do not implement one unless Гриша asks.

## Known notes (not automatic work)

- Funnel: most outbound calls are filtered out (duration, recording present, `context_type=2`).
- `MAX_COST_PER_CHAT_UNITS` was removed from `.env`; daily budget fell back to hardcoded 2_000_000 units. Confirm with Гриша before changing.
- `HEAD_CHAT_ID` in `.env` is empty; routing is by `employees` roles (`manager` / `head` / `owner`).

## Done recently (do not redo)

- Old Claude poller/stack disabled (`callbot*.service`).
- SQLite and manual recording upload stripped (`029e294`).
- Employee sync moved to `astra_worker.sync_all_employees()`.
