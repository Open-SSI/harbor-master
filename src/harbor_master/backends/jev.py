"""Jev, the decision model, through OpenRouter's Decisions API.

One request carries the state and any number of independent questions. Each answer is a
typed value with probabilities, not text. https://openrouter.ai/docs/guides/community/jev
"""
from __future__ import annotations

import asyncio
import functools
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from ..core import keys
from .base import NotImplementedBackend

URL = "https://openrouter.ai/api/alpha/decisions"
RETRY = {429, 500, 502, 503, 524, 529}
_POOL = ThreadPoolExecutor(max_workers=96, thread_name_prefix="jev")     # its own threads: many small requests at once


def _post(body: dict, key: str, timeout: float = 90) -> dict:
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


async def decisions(model: str, state, questions: dict, attempts: int = 4, timeout: float = 90) -> tuple[dict, float, int]:
    """Answers by question name, the cost in dollars, and the input tokens. `timeout` bounds each try."""
    key = keys.get("OPENROUTER_API_KEY")
    if not key:
        raise NotImplementedBackend("No OPENROUTER_API_KEY found for the decision tier. Add it to secrets.env.")
    body = {"model": model, "state": state, "questions": questions}
    for attempt in range(attempts):
        try:
            r = await asyncio.get_running_loop().run_in_executor(_POOL, functools.partial(_post, body, key, timeout))
            usage = r.get("usage") or {}
            return r["answers"], float(usage.get("cost") or 0.0), int(usage.get("input_tokens") or 0)
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:300]
            if err.code not in RETRY or attempt == attempts - 1:
                raise RuntimeError(f"Jev request failed with HTTP {err.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as err:
            if attempt == attempts - 1:
                raise RuntimeError(f"Jev request failed: {err}") from None
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError("Jev request failed")


def choice(question: str, criteria: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": question, "criteria": criteria}


def noul(question: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": question, "criteria": {"true": yes, "false": no}}
