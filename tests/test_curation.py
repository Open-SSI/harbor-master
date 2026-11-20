import random

from harbor_master.curation import difficulty, ranking


def known_order(n):
    """`n` items whose true order is known, a comparison that knows it, and a count of what was asked."""
    truth, counts = {f"t{i}": i for i in range(n)}, {"score": 0, "compare": 0}

    async def compare(a, b):
        counts["compare"] += 1
        return 0.95 if truth[a] > truth[b] else 0.05

    return truth, compare, counts


def test_stop_rule_matches_the_design():
    # 6 trials, 40%: a task may pass at most twice.
    assert difficulty.max_passes(6, 0.40) == 2
    assert difficulty.outcome(3, 1, 6, 0.40) == "drop"      # third pass: drop
    assert difficulty.outcome(0, 4, 6, 0.40) == "keep"      # fourth failure: keep
    assert difficulty.outcome(2, 2, 6, 0.40) is None        # still undecided after two rounds


def test_selection_cap_is_the_smaller_of_top_k_and_a_fraction():
    assert ranking.cap(1000, 24, 0.25) == 24        # a large benchmark sends top-k
    assert ranking.cap(60, 24, 0.25) == 15          # a small one sends a quarter of itself
    assert ranking.cap(70, 24, 0.25) == 18
    assert ranking.cap(2, 24, 0.25) == 1 and ranking.cap(0, 24, 0.25) == 0


def test_select_goes_round_robin_over_sub_areas():
    ranked = ["a1", "a2", "a3", "b1", "c1"]
    assert ranking.select(ranked, lambda x: x[0], 3) == ["a1", "b1", "c1"]
    assert ranking.select(ranked, lambda x: x[0], 3, stratify=False) == ["a1", "a2", "a3"]


async def test_pairwise_ranking_recovers_a_known_order():
    truth, compare, _ = known_order(30)
    ranked = [x for x, _ in await ranking.rank(list(truth), compare, per_item=12)]
    assert len(set(ranked[:10]) & {f"t{i}" for i in range(20, 30)}) >= 8
    assert len(set(ranked[-10:]) & {f"t{i}" for i in range(10)}) >= 8


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


async def test_ranking_falls_back_to_pairs_when_the_backend_cannot_score():
    truth, compare, _ = known_order(20)

    async def no_score(x):
        return None

    ranked = [x for x, _ in await ranking.rank_two_step(list(truth), no_score, compare, limit=5)]
    assert len(set(ranked[:5]) & {"t19", "t18", "t17", "t16", "t15"}) >= 4
