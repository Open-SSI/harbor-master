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
