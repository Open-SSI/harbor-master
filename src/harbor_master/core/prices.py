"""List prices, for costing a session or a trial from its tokens (DESIGN.md Section 2).

A cost that a provider reports is recorded as it is. Some report none: the Codex SDK, and an agent
on a subscription's sign-in. So that a run's budget and its statistics still mean something, the
cost recorded then is the tokens at the provider's published standard rate. That is an estimate at
list price, not an invoice: no discount, no long-context or batch rate.

Dollars per million tokens: (fresh input, cached input read, cache write, output). Read on 2026-11-19
from the providers' pricing pages. Where a provider gives no cache-write rate, 1.25 times the input
rate is used for Claude (the 5-minute rate where it is given), and the input rate elsewhere.
A model that is not here is not estimated: with no reported cost, its cost is recorded as 0.
"""
from __future__ import annotations

PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-2": (8.00, 0.20, 10.00, 40.00),
    "claude-fable-5-1": (10.00, 0.25, 12.50, 50.00),
    "claude-opus-5-5": (4.00, 0.20, 5.00, 20.00),
    "claude-sonnet-5-5": (2.00, 0.10, 2.50, 10.00),
    "gpt-6.2-astra": (8.00, 0.40, 8.00, 40.00),
    "gpt-6.2-sol": (2.00, 0.10, 2.00, 10.00),
    "gpt-6-astra": (10.00, 1.00, 10.00, 50.00),
    "gpt-6.1-sol": (2.00, 0.10, 2.00, 10.00),
    "gpt-6.1-terra": (1.00, 0.05, 1.00, 5.00),
    "gpt-6-sol": (2.00, 0.20, 2.00, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.10, 0.50),
    "gpt-5.6-sol": (4.00, 0.40, 4.00, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 2.00, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 0.20, 1.20),
}


def estimate(model: str, fresh_in: int = 0, cached: int = 0, cache_write: int = 0, out: int = 0) -> float | None:
    """The cost of these tokens at list price, in dollars. None when the model is not priced here."""
    rates = PER_MTOK.get(model)
    if rates is None:
        return None
    rate_in, rate_cached, rate_write, rate_out = rates
    return round((max(0, fresh_in) * rate_in + cached * rate_cached + cache_write * rate_write + out * rate_out) / 1e6, 6)
