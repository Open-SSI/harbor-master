"""Two contributors, two clones of one index: a benchmark is curated by one person at a time, and nobody runs a task twice."""
import subprocess
import time

import pytest

from harbor_master import ledger, tasks
from harbor_master.core import config as config_mod
from harbor_master.core import identity
from harbor_master.engine import Session, claims

from conftest import make_benchmark, write_index

BENCH = "demo-bench"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """An index that Alice owns and shares through a bare repository, which Bob joins. A way to act as either."""
    remote = tmp_path / "shared-index.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)], check=True)
    bench = make_benchmark(tmp_path / "fake", "demo-bench", 24)
    people: dict[str, dict] = {}

    def act_as(name: str) -> dict:
        monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / f"config-{name}"))
        if name not in people:
            who = identity.create(name.title(), "Some University", f"{name}@example.org", name)
            people[name] = {"ident": who, "ws": tmp_path / f"ws-{name}"}
        return people[name]

    alice, bob = act_as("alice"), act_as("bob")
    listed = [{"name": p["ident"].name, "github": p["ident"].github, "key": p["ident"].public_key,
               **({"role": "owner"} if name == "alice" else {})} for name, p in people.items()]
    cfg = config_mod.Config()
    cfg.select.require_brief = cfg.select.require_signoff = False
    cfg.select.top_k, cfg.select.max_fraction = 20, 1.0
    act_as("alice")
    write_index(alice["ws"], cfg, listed)
    start = ledger.Sync(alice["ws"], alice["ident"].name, "alice")
    start.define("start the index")
    start.connect(str(remote))
    act_as("bob")
    ledger.Sync.clone(str(remote), bob["ws"], bob["ident"].name, "bob")
    for p in people.values():
        tasks.register(p["ws"], BENCH, bench)

    def session(name: str, run_id: str) -> Session:
        p = act_as(name)
        return Session(p["ws"], config_mod.load(p["ws"]), p["ident"], tasks.read_registry(p["ws"]), simulate=True, as_real=True,
                       speed=0, no_input=True, run_id=run_id)

    return session, act_as


async def test_when_two_people_claim_at_once_the_second_push_loses_and_gives_way(world):
    session, act_as = world
    alice, bob = session("alice", "a1"), session("bob", "b1")

    async def stale_pull():                                         # Bob looks at the ledger before Alice pushes
        return None

    act_as("alice")
    assert await claims.claim(alice, alice.benches[BENCH]) == ""
    act_as("bob")
    bob.pull, real_pull = stale_pull, bob.pull
    assert await claims.claim(bob, bob.benches[BENCH]) == "alice"   # his push is refused, he pulls, and gives way
    await real_pull()
    assert not bob.claimed[BENCH] and alice.claimed[BENCH] == [claims.BENCH]
    assert bob.ledger.claims(BENCH, time.time(), me="bob")[claims.BENCH][0] == "alice"
