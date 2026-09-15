"""Точка входа для systemd. Держим её отдельно от queue_runner.py, чтобы
модуль всегда импортировался под именем "queue_runner" — см. комментарий в
конце queue_runner.py про дублирование модуля при прямом запуске."""

import asyncio

from queue_runner import main

if __name__ == "__main__":
    asyncio.run(main())
