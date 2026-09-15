"""
Распознавание речи через Deepgram — общий модуль для demo_bot.py (ручной
аплоад) и worker.py (автосбор из Mango). Диаризация средствами Deepgram
(diarize=true), ffmpeg не нужен.
"""

import os

import httpx

DG_MODEL = os.getenv("DG_MODEL", "nova-2")
DG_LANG = os.getenv("DG_LANG", "ru")

# Общий клиент на процесс — см. mango_client.py для обоснования (та же
# защита от роста памяти на долгих циклах create/destroy httpx-клиента).
_client = httpx.AsyncClient(timeout=300)


async def close() -> None:
    await _client.aclose()


async def transcribe(path: str) -> tuple[str, float]:
    """Возвращает транскрипт с разделением говорящих и длительность в секундах."""
    with open(path, "rb") as f:
        content = f.read()
    return await transcribe_bytes(content)


async def transcribe_bytes(content: bytes) -> tuple[str, float]:
    """То же самое, но без файла на диске — запись из Mango worker.py качает
    прямо в память и передаёт сюда, не сохраняя аудио (см. критические детали
    промта: скачал, распознал, удалил)."""
    r = await _client.post(
        "https://api.deepgram.com/v1/listen",
        headers={
            "Authorization": f"Token {os.environ['DEEPGRAM_API_KEY']}",
            "Content-Type": "audio/*",
        },
        params={
            "model": DG_MODEL,
            "language": DG_LANG,
            "diarize": "true",
            "utterances": "true",
            "punctuate": "true",
            "smart_format": "true",
        },
        content=content,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Deepgram {r.status_code}: {r.text}")
    data = r.json()

    duration = data["metadata"]["duration"]
    utterances = data["results"].get("utterances", [])

    if not utterances:
        text = data["results"]["channels"][0]["alternatives"][0]["transcript"]
        return text, duration

    lines, prev = [], None
    for u in utterances:
        spk = u.get("speaker", 0)
        if spk != prev:
            mm, ss = divmod(int(u.get("start", 0) or 0), 60)
            # Таймкод — от начала блока (первой реплики говорящего), не от
            # каждой отдельной фразы: склейка реплик подряд сохраняется ради
            # читаемости и меньшего объёма транскрипта (см. prompt_reports_final,
            # п.1) — указывает место в записи с точностью до нескольких секунд,
            # этого достаточно, чтобы найти цитату.
            lines.append(f"\n[{mm:02d}:{ss:02d} Спикер {spk}] {u['transcript']}")
            prev = spk
        else:
            lines[-1] += " " + u["transcript"]
    return "\n".join(lines).strip(), duration
