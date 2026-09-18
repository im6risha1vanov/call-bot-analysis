"""
Синтез речи для голосового тренажёра (Этап 4).

Провайдер выбирается через TTS_PROVIDER в .env. Реализован yandex —
Yandex SpeechKit: он умеет отдавать сразу oggopus, то есть ровно тот формат,
который Telegram ждёт в send_voice, и конвертация через ffmpeg не нужна.

Уже подключённый Astra-эндпоинт (ai.starimg.ru) синтез не поддерживает
(/v1/audio/speech → 404), поэтому это отдельный сервис со своим ключом.

Если провайдер не настроен — synthesize_ogg() бросает TTSNotConfigured, и
вызывающий код (training_bot) откатывается на текстовый ответ, а не делает вид,
что голос работает.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("tts")

YANDEX_TTS_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
YANDEX_TEXT_LIMIT = 5000

# Оценка для бюджетного потолка и логов, не счёт. Реальный расход — в консоли
# Yandex Cloud; порядок ~0.4 ₽ за 1000 символов, пересчёт в USD задаётся
# переменной, чтобы курс и тариф можно было поправить без правки кода.
USD_PER_1K_CHARS = float(os.getenv("TTS_USD_PER_1K_CHARS", "0.0045"))

_client: httpx.AsyncClient | None = None


class TTSNotConfigured(Exception):
    """Ни один провайдер синтеза речи не настроен — см. TTS_PROVIDER в .env."""


class TTSError(Exception):
    """Провайдер настроен, но запрос не удался (сеть/квота/ошибка API)."""


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _yandex_synthesize(text: str) -> bytes:
    api_key = os.getenv("YANDEX_TTS_API_KEY", "").strip()
    if not api_key:
        raise TTSNotConfigured("TTS_PROVIDER=yandex, но YANDEX_TTS_API_KEY не задан")

    voice = os.getenv("YANDEX_TTS_VOICE", "filipp").strip()
    data = {
        "text": text,
        "lang": "ru-RU",
        "voice": voice,
        "format": "oggopus",  # нативный формат Telegram voice — без ffmpeg
    }
    # Каталог указывать не нужно: ключ сервисного аккаунта уже привязан к нему
    # (проверено на боевом ключе). Переменная оставлена на случай ключа от
    # пользовательского аккаунта, где folderId обязателен.
    folder_id = os.getenv("YANDEX_TTS_FOLDER_ID", "").strip()
    if folder_id:
        data["folderId"] = folder_id

    r = await _http().post(
        YANDEX_TTS_URL,
        headers={"Authorization": f"Api-Key {api_key}"},
        data=data,
    )
    if r.status_code != 200:
        # Не raise_for_status(): тело ответа Яндекса содержит причину отказа
        # (квота, роль сервисного аккаунта, неизвестный голос) — без него
        # отладка превращается в гадание.
        raise TTSError(f"Yandex SpeechKit {r.status_code}: {r.text[:700]}")
    if not r.content:
        raise TTSError("Yandex SpeechKit вернул пустой ответ при статусе 200")
    return r.content


async def synthesize_ogg(text: str) -> tuple[bytes, float]:
    """Возвращает (ogg_opus_bytes, оценка_стоимости_usd)."""
    provider = os.getenv("TTS_PROVIDER", "").strip().lower()
    if not provider:
        raise TTSNotConfigured("TTS_PROVIDER не задан в .env — провайдер синтеза речи не выбран")

    if len(text) > YANDEX_TEXT_LIMIT:
        log.warning("реплика длиннее лимита TTS (%s > %s), обрезаю", len(text), YANDEX_TEXT_LIMIT)
        text = text[:YANDEX_TEXT_LIMIT]

    if provider == "yandex":
        audio = await _yandex_synthesize(text)
    else:
        raise TTSNotConfigured(f"провайдер TTS_PROVIDER={provider!r} объявлен, но не реализован")

    cost = len(text) / 1000 * USD_PER_1K_CHARS
    log.info("TTS %s: символов=%s, байт=%s, оценка $%.5f", provider, len(text), len(audio), cost)
    return audio, cost
