"""
Универсальный раннер очереди задач (Этап 1 мультиагентной спецификации).

Один процесс берёт задачу любого зарегистрированного типа через
SELECT ... FOR UPDATE SKIP LOCKED и вызывает обработчик по типу из реестра.
Обработчик про очередь ничего не знает — принимает pool и Record задачи,
возвращает dict (пишется в tasks.result) или бросает исключение.

Добавление нового агента впоследствии = новый файл в handlers/ с
@register("тип"), а не новый воркер с собственным циклом опроса.
"""

import asyncio
import json
import logging
import os
from datetime import timedelta
from typing import Awaitable, Callable

import asyncpg

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("queue-runner")

POLL_INTERVAL_SEC = 5
BACKOFF_MINUTES = [2, 10, 30, 60, 120]

Handler = Callable[[asyncpg.Pool, asyncpg.Record], Awaitable[dict | None]]
StartupHook = Callable[[asyncpg.Pool], Awaitable[None]]

_REGISTRY: dict[str, Handler] = {}
_STARTUP_HOOKS: list[StartupHook] = []


def register(task_type: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        if task_type in _REGISTRY:
            raise RuntimeError(f"обработчик для {task_type!r} уже зарегистрирован")
        _REGISTRY[task_type] = fn
        return fn
    return deco


def on_startup(fn: StartupHook) -> StartupHook:
    """Хук восстановления после рестарта — например, обработчик может держать
    статус 'processing' в своих собственных таблицах (не только в tasks) и
    должен сбросить зависшие строки при старте раннера."""
    _STARTUP_HOOKS.append(fn)
    return fn


class RetryLater(Exception):
    """Обработчик бросает это, если задачу нужно повторить позже без учёта в
    счётчике попыток — например, дневной бюджет клиента ещё не сброшен. Это
    отложенное состояние, а не сбой самой задачи."""

    def __init__(self, delay: timedelta, reason: str = ""):
        super().__init__(reason)
        self.delay = delay


async def reset_orphaned(pool: asyncpg.Pool) -> None:
    result = await pool.execute("UPDATE tasks SET status='new', updated_at=now() WHERE status='processing'")
    if result != "UPDATE 0":
        log.warning("восстановлены зависшие processing-задачи после перезапуска: %s", result)
    for hook in _STARTUP_HOOKS:
        await hook(pool)


async def claim_next(pool: asyncpg.Pool) -> asyncpg.Record | None:
    if not _REGISTRY:
        return None
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT * FROM tasks
                WHERE status = 'new' AND run_after <= now()
                  AND type = ANY($1::text[])
                ORDER BY run_after, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                list(_REGISTRY.keys()),
            )
            if row:
                await conn.execute("UPDATE tasks SET status='processing', updated_at=now() WHERE id=$1", row["id"])
    return row


async def _fail_or_retry(pool: asyncpg.Pool, task: asyncpg.Record, exc: Exception) -> None:
    attempts = task["attempts"] + 1
    if attempts >= task["max_attempts"]:
        log.error("задача id=%s type=%s провалена окончательно после %s попыток: %s",
                   task["id"], task["type"], attempts, exc)
        await pool.execute(
            "UPDATE tasks SET status='failed', attempts=$2, error=$3, updated_at=now() WHERE id=$1",
            task["id"], attempts, str(exc)[:2000],
        )
        return
    delay = timedelta(minutes=BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)])
    log.warning("задача id=%s type=%s попытка %s не удалась (%s), повтор через %s",
                task["id"], task["type"], attempts, exc, delay)
    await pool.execute(
        "UPDATE tasks SET status='new', attempts=$2, run_after=now()+$3, error=$4, updated_at=now() WHERE id=$1",
        task["id"], attempts, delay, str(exc)[:2000],
    )


async def run_once(pool: asyncpg.Pool) -> bool:
    """Возвращает True, если задача была найдена (успешно обработана или нет)."""
    task = await claim_next(pool)
    if task is None:
        return False

    handler = _REGISTRY[task["type"]]
    try:
        result = await handler(pool, task)
        await pool.execute(
            "UPDATE tasks SET status='done', result=$2::jsonb, updated_at=now() WHERE id=$1",
            task["id"], json.dumps(result or {}, ensure_ascii=False, default=str),
        )
    except RetryLater as exc:
        log.info("задача id=%s type=%s отложена: %s (через %s)", task["id"], task["type"], exc, exc.delay)
        await pool.execute(
            "UPDATE tasks SET status='new', run_after=now()+$2, updated_at=now() WHERE id=$1",
            task["id"], exc.delay,
        )
    except Exception as exc:
        log.exception("ошибка обработки задачи id=%s type=%s", task["id"], task["type"])
        await _fail_or_retry(pool, task, exc)
    return True


async def main() -> None:
    # Импорт регистрирует обработчики декоратором @register — сам раннер про
    # конкретные типы задач ничего не знает.
    import handlers.analyze_call  # noqa: F401

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=5)
    await reset_orphaned(pool)
    log.info("queue runner started, обработчики: %s", sorted(_REGISTRY))
    try:
        while True:
            handled = await run_once(pool)
            if not handled:
                await asyncio.sleep(POLL_INTERVAL_SEC)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
