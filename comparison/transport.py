"""Shared generation settings; never log credentials, transcript or provider error bodies."""
from __future__ import annotations
import httpx
import analysis


class RequestFailure(Exception):
    def __init__(self, status=None):
        self.status = status
        super().__init__(f'provider HTTP {status}' if status else 'provider result unknown')


def settings():
    return {'model': analysis.MODEL, 'max_output_tokens': 6000,
            'reasoning': {'effort': analysis.REASONING_EFFORT},
            'timeout_seconds': analysis.TIMEOUT, 'multiplier': analysis.ASTRA_MULTIPLIER,
            'cache_fraction': analysis.CACHE_READ_FRACTION,
            'seed': None, 'seed_note': 'Endpoint has no confirmed seed support; not requested',
            'legacy_verdict': {'keys': sorted(analysis.LEVEL_CRITICAL_CRITERIA),
                              'min_failures': analysis.VERDICT_MIN_FAILURES,
                              'min_with_signal': analysis.VERDICT_MIN_FAILURES_WITH_SIGNAL}}


async def request(prompt, user, params):
    body = {k: params[k] for k in ('model', 'max_output_tokens', 'reasoning')}
    body.update(instructions=prompt, input=user)
    try:
        async with httpx.AsyncClient(timeout=params['timeout_seconds']) as client:
            response = await client.post(analysis.CVC_BASE_URL + '/responses', json=body,
                                         headers={'Authorization': 'Bearer ' + analysis.CVC_KEY})
    except httpx.HTTPError as exc:
        raise RequestFailure() from exc
    if response.status_code >= 300:
        raise RequestFailure(response.status_code)
    try:
        data = response.json()
        text = data.get('output_text') or ''.join(
            p.get('text', '') for item in data.get('output', []) if item.get('type') == 'message'
            for p in item.get('content', []) if p.get('type') == 'output_text')
        usage = data.get('usage') or {}
        details = usage.get('input_tokens_details') or {}
        cached = details.get('cached_tokens', 0); written = details.get('cache_write_tokens', 0)
        cost = (max(usage.get('input_tokens', 0)-cached-written, 0)
                + cached*params['cache_fraction']+written+usage.get('output_tokens', 0))*params['multiplier']
        return text, cost, usage
    except (ValueError, TypeError, AttributeError) as exc:
        raise RequestFailure() from exc
