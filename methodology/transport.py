"""One paid extraction request. Persist the receipt before validating the model JSON."""
from __future__ import annotations

import httpx

import analysis


class ProviderFailure(Exception):
    def __init__(self, status=None):
        self.status = status
        super().__init__(f"Astra methodology HTTP {status}" if status else "Astra methodology network failure")


async def request(prompt: str, transcript: str, context: dict) -> tuple[str, float]:
    import json
    body = {"model": analysis.MODEL, "instructions": prompt,
            "input": "КОНТЕКСТ (не заменяет цитаты):\n" + json.dumps(context, ensure_ascii=False, default=str)
                     + "\n\nТРАНСКРИПТ:\n" + transcript,
            "max_output_tokens": 6000, "reasoning": {"effort": analysis.REASONING_EFFORT}}
    try:
        async with httpx.AsyncClient(timeout=analysis.TIMEOUT) as client:
            response = await client.post(analysis.CVC_BASE_URL + "/responses", json=body,
                                         headers={"Authorization": "Bearer " + analysis.CVC_KEY})
    except httpx.HTTPError as exc:
        raise ProviderFailure() from exc
    if response.status_code >= 300:
        raise ProviderFailure(response.status_code)
    try:
        data = response.json()
        text = data.get("output_text") or "".join(
            p.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
            for p in item.get("content", []) if p.get("type") == "output_text")
        return text, analysis._cost(data.get("usage") or {})
    except (ValueError, TypeError, AttributeError) as exc:
        # Unknown provider result: never automatically pay a second time.
        raise ProviderFailure() from exc
