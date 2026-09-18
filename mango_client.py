"""
Общие вызовы Mango Office VPBX API — опрос звонков и справочник сотрудников
(astra_worker.py), скачивание записи (handlers/analyze_call.py).

Подпись — sha256(vpbx_api_key + json + vpbx_api_salt), см.
support/.../2_4_3_o_parametre_sign/. json — компактная строка без пробелов.
"""

import hashlib
import json

import httpx

BASE_URL = "https://app.mango-office.ru/vpbx"

# Общий клиент на процесс, а не новый на каждый вызов. astra_worker опрашивает
# Mango раз в 5 минут — за сутки набегает сотни короткоживущих клиентов
# подряд (свой SSL-контекст, буферы, TCP-хендшейк на каждый), что
# раздувает память процесса без настоящей утечки Python-ссылок — типичная
# фрагментация аллокатора на длинных циклах create/destroy. Обнаружено по
# факту: воркер съел 1.1ГБ за 20 часов и был убит OOM-killer'ом на сервере
# без подкачки. Закрывается через close() при остановке процесса.
_client = httpx.AsyncClient(timeout=30.0)


async def close() -> None:
    await _client.aclose()


def sign(vpbx_api_key: str, raw_json: str, salt: str) -> str:
    return hashlib.sha256((vpbx_api_key + raw_json + salt).encode("utf-8")).hexdigest()


async def call(method: str, vpbx_api_key: str, salt: str, payload: dict, timeout: float = 30.0) -> dict:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    data = {"vpbx_api_key": vpbx_api_key, "sign": sign(vpbx_api_key, raw, salt), "json": raw}
    r = await _client.post(f"{BASE_URL}/{method}", data=data, timeout=timeout)
    if r.status_code >= 300:
        raise RuntimeError(f"Mango {method} {r.status_code}: {r.text[:500]}")
    return r.json()


async def fetch_employees(vpbx_api_key: str, salt: str) -> list[dict]:
    """POST /config/users/request без extension — возвращает всех сотрудников ВАТС."""
    data = await call("config/users/request", vpbx_api_key, salt, {})
    return data.get("users", [])


async def fetch_recording(vpbx_api_key: str, salt: str, recording_id: str, timeout: float = 60.0) -> bytes:
    """POST /queries/recording/post — не JSON, а 302 с Location на файл (см.
    руководство VPBX API п.3.5.1); сам файл отдаёт вторым обычным GET. Ссылка
    одноразовая без параметра времени жизни — качать сразу, как взяли
    задачу в работу, а не откладывать."""
    payload = {"recording_id": recording_id, "action": "download"}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    data = {"vpbx_api_key": vpbx_api_key, "sign": sign(vpbx_api_key, raw, salt), "json": raw}
    r = await _client.post(f"{BASE_URL}/queries/recording/post", data=data, timeout=timeout, follow_redirects=False)
    if r.status_code != 302:
        raise RuntimeError(f"Mango recording/post {r.status_code}: {r.text[:500]}")
    file_resp = await _client.get(r.headers["location"], timeout=timeout)
    if file_resp.status_code != 200:
        raise RuntimeError(f"Mango file download {file_resp.status_code}")
    return file_resp.content
