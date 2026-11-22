"""Ranking tasks by pairwise comparison, and the selection cap (DESIGN.md Section 4)."""
from __future__ import annotations

import asyncio
import math
import random
from typing import Awaitable, Callable, Hashable, Sequence

Compare = Callable[[Hashable, Hashable], Awaitable[float]]  # probability that the first matters more


def bradley_terry(n: int, results: list[tuple[int, int, float]], iterations: int = 200) -> list[float]:
    """Log-strengths from soft pairwise results (i, j, probability that i beats j)."""
    wins = [0.5] * n            # a weak prior: half a win and half a loss against an average item
    pairs: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for i, j, p in results:
        wins[i] += p
        wins[j] += 1 - p
        pairs[i].append((j, 1.0))
        pairs[j].append((i, 1.0))
    w = [1.0] * n
    for _ in range(iterations):
        new = []
        for i in range(n):
            denom = 1.0 / (w[i] + 1.0) + sum(c / (w[i] + w[j]) for j, c in pairs[i])
            new.append(wins[i] / denom)
        mean = math.exp(sum(math.log(x) for x in new) / n)
        new = [x / mean for x in new]
        if max(abs(a - b) for a, b in zip(new, w)) < 1e-9:
            w = new
            break
        w = new
    return [math.log(x) for x in w]


def pairs_at_once(n: int, per_item: int, rng: random.Random) -> list[tuple[int, int]]:
    """Every pair to compare, chosen before any is asked, for items listed best first by a score: each meets its
    neighbours in that list (where the order matters most, and is least sure) and a few others at random, so that a
    task the score misplaced can still move far. About `per_item` each, every pair once."""
    if n <= 1:
        return []
    far = min(2, n - 1)                         # each item picks this many at random, and is picked about as often
    near = max(1, (per_item - 2 * far) // 2)    # and meets this many on each side of it in the list
    pairs: set[tuple[int, int]] = set()
    for i in range(n):
        for j in range(i + 1, min(n, i + near + 1)):
            pairs.add((i, j))
    for i in range(n):
        others = [j for j in range(n) if j != i]
        for j in rng.sample(others, min(far, len(others))):
            pairs.add((min(i, j), max(i, j)))
    out = sorted(pairs)
    rng.shuffle(out)
    return [(a, b) if rng.random() < 0.5 else (b, a) for a, b in out]


async def rank_at_once(items: Sequence[Hashable], compare: Compare, per_item: int = 12, seed: int = 0,
                       concurrency: int = 32) -> list[tuple[Hashable, float]]:
    """Each item, listed best first by a score, meets about `per_item` others, all asked side by side in one go: no
    round waits for the slowest answer of the round before it."""
    n = len(items)
    if n <= 1:
        return [(x, 0.0) for x in items]
    limit = asyncio.Semaphore(concurrency)

    async def one(a: int, b: int) -> tuple[int, int, float]:
        async with limit:
            return a, b, await compare(items[a], items[b])

    results = await asyncio.gather(*(one(a, b) for a, b in pairs_at_once(n, per_item, random.Random(seed))))
    return sorted(zip(items, bradley_terry(n, list(results))), key=lambda x: -x[1])


async def rank(items: Sequence[Hashable], compare: Compare, per_item: int = 12,
               seed: int = 0, concurrency: int = 16) -> list[tuple[Hashable, float]]:
    """Each item meets about `per_item` others, in rounds that pair items of similar rating. With no score to start
    from, the rounds find who is near whom."""
    n = len(items)
    if n <= 1:
        return [(x, 0.0) for x in items]
    rng = random.Random(seed)
    limit = asyncio.Semaphore(concurrency)
    results: list[tuple[int, int, float]] = []
    seen: set[tuple[int, int]] = set()
    ratings = [0.0] * n
    for round_no in range(min(per_item, n - 1)):
        order = list(range(n))
        if round_no == 0:
            rng.shuffle(order)
        else:
            order.sort(key=lambda i: -ratings[i])
            if round_no % 2 and n > 2:      # shift by one so neighbors change between rounds
                order = order[1:] + order[:1]
        pairs = []
        for a, b in zip(order[::2], order[1::2]):
            if (min(a, b), max(a, b)) not in seen:
                seen.add((min(a, b), max(a, b)))
                pairs.append((a, b))

        async def one(a: int, b: int) -> tuple[int, int, float]:
            async with limit:
                return a, b, await compare(items[a], items[b])

        results += await asyncio.gather(*(one(a, b) for a, b in pairs))      # a round's pairs do not depend on each other
        ratings = bradley_terry(n, results)
    return sorted(zip(items, ratings), key=lambda x: -x[1])


Score = Callable[[Hashable], Awaitable[float | None]]      # one task's importance, or None if the backend cannot score


async def rank_two_step(items: Sequence[Hashable], score: Score, compare: Compare, limit: int, per_item: int = 12,
                        pool_factor: int = 3, concurrency: int = 16) -> list[tuple[Hashable, float]]:
    """Score every item once, then compare in pairs only the ones near the top.

    Only the order around the cut has to be exact, and the cut is at `limit`. So the pairwise
    work, which is the costly part, runs on the best `pool_factor x limit` items by score, and
    the rest keep their order by score, below the pool. Falls back to comparing everything in
    pairs when the backend cannot score.
    """
    n = len(items)
    if n <= 1:
        return [(x, 0.0) for x in items]
    gate = asyncio.Semaphore(concurrency)

    async def one(x: Hashable) -> float | None:
        async with gate:
            return await score(x)

    scores = await asyncio.gather(*(one(x) for x in items))
    if any(v is None for v in scores):
        return await rank(items, compare, per_item, concurrency=concurrency)
    order = sorted(range(n), key=lambda i: -scores[i])
    pool_size = min(n, max(pool_factor * limit, limit + per_item))
    pool = await rank_at_once([items[i] for i in order[:pool_size]], compare, per_item, concurrency=concurrency)
    floor = min((r for _, r in pool), default=0.0)
    top = scores[order[pool_size - 1]]
    rest = [(items[i], floor - 1.0 - (top - scores[i])) for i in order[pool_size:]]     # below the pool, by score
    return [*pool, *rest]


def positions(text: str, n: int, most: int) -> tuple[list[int], str]:
    """The ranked positions a person typed, such as "1, 2, 3, 5" or "1-13, 16", in rank order, and "" when they hold;
    else an empty list and why not. Each is a position from 1 to n, and at most `most` of them."""
    picked: set[int] = set()
    for part in text.replace(",", " ").split():
        low, dash, high = part.partition("-")
        if not low.isdigit() or (dash and not high.isdigit()):
            return [], f"{part} is not a position: type the numbers of the tasks that go on, such as 1, 2, 3, 5"
        a, b = int(low), int(high) if dash else int(low)
        if a > b:
            a, b = b, a
        if a < 1 or b > n:
            return [], f"{part} is not on the list: positions go from 1 to {n}"
        picked.update(range(a, b + 1))
    if not picked:
        return [], "type the numbers of the tasks that go on, such as 1, 2, 3, 5"
    if len(picked) > most:
        return [], f"that is {len(picked)} tasks: at most {most} may go on"
    return sorted(picked), ""


def cap(n_total: int, top_k: int, max_fraction: float) -> int:
    """How many tasks of a benchmark go on: the smaller of top-k and a fraction of the benchmark.

    So no benchmark sends more than top-k tasks, and a small one sends at most that fraction of itself.
    At least one task goes on when there is any.
    """
    return min(n_total, max(1, min(top_k, math.ceil(max_fraction * n_total)))) if n_total else 0


def select(ranked: Sequence[Hashable], subarea: Callable[[Hashable], str], limit: int,
           stratify: bool = True) -> list[Hashable]:
    """Take `limit` items in rank order, round-robin over sub-areas when stratifying."""
    if not stratify:
        return list(ranked[:limit])
    groups: dict[str, list[Hashable]] = {}
    for item in ranked:
        groups.setdefault(subarea(item), []).append(item)
    chosen: list[Hashable] = []
    while len(chosen) < limit and any(groups.values()):
        for key in list(groups):
            if groups[key] and len(chosen) < limit:
                chosen.append(groups[key].pop(0))
    return chosen
