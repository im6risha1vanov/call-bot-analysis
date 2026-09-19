# AI_INSTRUCTIONS

Правила для других агентов, которые клонируют этот репозиторий. Owner: Гриша Иванов.

Rules for other agents cloning this repo. Owner: Гриша Иванов.

## Before any work / Перед работой

1. **Read these four docs first** (in order): `README.md`, `PROJECT_CONTEXT.md`, `CURRENT_TASK.md`, `DECISIONS.md`. Then this file.
2. **Pull from GitHub before work.** `git fetch` + `git pull` on the branch you will use so you do not overwrite newer commits.
3. Work only in `/opt/callbot-astra` on **`bot-server`** (`9015421-nt422325`). If the hostname is `ams-1-vm-cvf5` or the project is `bt-dispatch-bot`, **stop**. Do not touch `/opt/callbot`.

## During work / В работе

4. **Significant work goes in a git branch.** Do not pile unrelated changes on `main`/`master`. Branch names should be descriptive.
5. Do not delete or overwrite existing files without confirmation from Гриша. Adding missing docs is allowed; do not rewrite these five files if they already exist — report contents and ask.
6. **No irreversible actions without confirmation** (drop tables, disable prod units, delete recordings, force-push, rewrite history, rotate keys, change DNS, `systemctl disable` of the live Astra units, etc.).
7. Secret scan before commit: working tree **and** git history. Never commit `.env`, `.venv`, keys, credentials, call recordings, sqlite dumps.
8. Do not print `.env` values. Do not paste API keys into chat. Do not put tokens in `git remote` URLs. Prefer `gh` + SSH or `gh` as the logged-in user.

## After work / После работы

9. **Update these docs** if goal, status, remaining work, or decisions changed.
10. **Commit with a meaningful message and push** the branch (and `main` only when that is the agreed publish/merge).
11. Leave the four Astra processes running unless Гриша asked to stop them.

## Sync checklist

```text
[ ] read README, PROJECT_CONTEXT, CURRENT_TASK, DECISIONS
[ ] git pull
[ ] branch for significant work
[ ] no secrets in the commit
[ ] update docs
[ ] push
[ ] no irreversible action without confirmation
```
