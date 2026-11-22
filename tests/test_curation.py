import random

from harbor_master.curation import ranking


def known_order(n):
    """`n` items whose true order is known, a comparison that knows it, and a count of what was asked."""
    truth, counts = {f"t{i}": i for i in range(n)}, {"score": 0, "compare": 0}

    async def compare(a, b):
        counts["compare"] += 1
        return 0.95 if truth[a] > truth[b] else 0.05

    return truth, compare, counts


async def test_two_step_ranking_finds_the_top_with_far_fewer_comparisons():
    (truth, compare, counts), rng = known_order(400), random.Random(1)

    async def score(x):
        counts["score"] += 1
        return truth[x] / 100 + rng.uniform(-0.4, 0.4)           # a noisy first look

    ranked = [x for x, _ in await ranking.rank_two_step(list(truth), score, compare, limit=24)]
    assert len(set(ranked[:24]) & {f"t{i}" for i in range(376, 400)}) >= 21 and len(ranked) == 400     # the real top 24, give or take the noise
    assert counts["score"] == 400 and counts["compare"] <= 72 * 12 / 2     # pairs only among the best 72
    counts["compare"] = 0
    await ranking.rank(list(truth), compare)
    assert counts["compare"] >= 2000                                        # the old way compared everything


async def test_ranking_asks_every_pair_at_once_so_one_slow_answer_holds_up_nothing_else():
    import asyncio
    import time
    truth = {f"t{i}": i for i in range(51)}
    slow = {"first": True}

    async def compare(a, b):
        if slow["first"]:                       # one answer takes long: the others do not wait for it in rounds
            slow["first"] = False
            await asyncio.sleep(1.0)
        else:
            await asyncio.sleep(0.05)
        return 0.9 if truth[a] > truth[b] else 0.1

    async def score(x):
        return truth[x] + random.Random(x).uniform(-3, 3)
    started = time.monotonic()
    ranked = [x for x, _ in await ranking.rank_two_step(list(truth), score, compare, limit=17, concurrency=32)]
    assert time.monotonic() - started < 2.0                 # about 300 comparisons, in about the time of the slowest
    assert len(set(ranked[:17]) & {f"t{i}" for i in range(34, 51)}) >= 15


def test_a_selection_is_the_positions_a_person_types():
    assert ranking.positions("1, 2, 3, 5", 55, 18) == ([1, 2, 3, 5], "")
    assert ranking.positions("1-13, 16", 55, 18) == ([*range(1, 14), 16], "")
    assert ranking.positions("16 3 3 1-2", 55, 18) == ([1, 2, 3, 16], "")          # any order, each once
    assert "at most 18" in ranking.positions("1-19", 55, 18)[1]
    assert "not on the list" in ranking.positions("56", 55, 18)[1]
    assert "not a position" in ranking.positions("top 5", 55, 18)[1]
