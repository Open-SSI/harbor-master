"""Two contributors, two clones of one index: nobody runs the same task twice."""
import subprocess
import time

import pytest
import tomli_w

from harbor_master import ledger, tasks
from harbor_master.core import config as config_mod
from harbor_master.core import identity
from harbor_master.engine import Session, claims, shared

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

    for name in ("alice", "bob"):
        act_as(name)
    listed = [{"name": p["ident"].name, "github": p["ident"].github, "key": p["ident"].public_key,
               **({"role": "owner"} if name == "alice" else {})} for name, p in people.items()]
    cfg = config_mod.Config()
    cfg.select.require_brief = cfg.select.require_signoff = False
    cfg.select.top_k, cfg.select.max_fraction = 20, 1.0
    alice = act_as("alice")
    write_index(alice["ws"], cfg, listed)
    start = ledger.Sync(alice["ws"], alice["ident"].name, "alice")
    start.define("start the index")
    start.connect(str(remote))
    bob = act_as("bob")
    ledger.Sync.clone(str(remote), bob["ws"], bob["ident"].name, "bob")
    for p in people.values():
        tasks.register(p["ws"], BENCH, bench)

    def session(name: str, run_id: str) -> Session:
        p = act_as(name)
        return Session(p["ws"], config_mod.load(p["ws"]), p["ident"], tasks.read_registry(p["ws"]), simulate=True, as_real=True,
                       speed=0, no_input=True, run_id=run_id)

    return session, act_as


def notes(s: Session) -> str:
    return " | ".join(text for _, kind, _, text in s.recent if kind == "rejected")


async def test_a_running_task_and_a_completed_task_are_both_rejected_for_the_other_person(world):
    session, act_as = world
    alice = session("alice", "a1")
    await shared.run_shared(alice, alice.benches[BENCH])            # Alice claims and pushes, then is "running"
    assert len(alice.claimed[BENCH]) == 20

    bob = session("bob", "b1")
    await bob.run()                                                 # Bob pulls first, and sees her claims
    assert bob.benches[BENCH].selected == 0 and "being run by @alice" in notes(bob)
    assert not any(e["type"] == "trial" and e["runner"] == "bob" for e in bob.ledger.events)

    act_as("alice")
    alice._workers = [__import__("asyncio").create_task(alice._cell_loop(c)) for c in alice.cells]
    await __import__("asyncio").gather(*alice._workers)             # Alice finishes: records pushed, claims released
    assert not alice.claimed[BENCH]

    bob2 = session("bob", "b2")
    await bob2.run()                                                # now they are completed, and still rejected
    assert bob2.benches[BENCH].selected == 0 and "already completed" in notes(bob2)
    assert not any(e["type"] == "trial" and e["runner"] == "bob" for e in bob2.ledger.events)
    assert ledger.verify(bob2.ledger_dir) == []                     # Bob's copy now holds both people's records, all valid
    from harbor_master import interact                              # and the reports Alice's run wrote, so that he can review them
    theirs = sorted((bob2.ws / "reports" / BENCH).glob("*.md"))
    assert theirs and bob2.ready and "Recommendation" in interact.execute(bob2, interact.Command("report", [bob2.ready[0].name]))


async def test_an_interrupted_or_failed_task_is_released_for_others(world, monkeypatch):
    session, act_as = world
    alice = session("alice", "a1")
    await shared.run_shared(alice, alice.benches[BENCH])
    stopped, failed = alice.queue[0], alice.queue[1]

    async def boom(s, cell, task):
        raise RuntimeError("docker went away")

    monkeypatch.setattr("harbor_master.engine.stages.process", boom)     # every task Alice starts now fails
    alice.queue[:] = [failed]
    alice.active_cells = 1
    await alice._cell_loop(alice.cells[0])
    assert failed.task_id not in alice.claimed[BENCH]               # a failed task is let go at once
    alice.release(BENCH, [stopped.task_id])                         # and so is one that is stopped
    await alice.publish("stopped one")
    monkeypatch.undo()

    act_as("bob")
    bob = session("bob", "b1")
    await bob.pull()
    held = bob.ledger.claims(BENCH, time.time(), me="bob")
    assert failed.task_id not in held and stopped.task_id not in held
    assert len(held) == 18                                          # the rest are still Alice's


async def test_when_two_people_claim_at_once_the_second_push_loses_and_gives_way(world):
    session, act_as = world
    alice, bob = session("alice", "a1"), session("bob", "b1")
    for s, name in ((alice, "alice"), (bob, "bob")):                # both load and select, without touching the remote
        act_as(name)
        s.benches[BENCH].tasks = tasks.load_benchmark(s.benches[BENCH].path, BENCH)
    chosen_a, chosen_b = alice.benches[BENCH].tasks[:10], bob.benches[BENCH].tasks[5:15]

    async def stale_pull():                                         # Bob looks at the ledger before Alice pushes
        return None

    act_as("alice")
    got_a = await claims.take(alice, alice.benches[BENCH], chosen_a)
    act_as("bob")
    bob.pull, real_pull = stale_pull, bob.pull
    got_b = await claims.take(bob, bob.benches[BENCH], chosen_b)    # his push is refused, he pulls, and gives way
    assert len(got_a) == 10
    assert {t.name for t in got_b} == {t.name for t in chosen_b[5:]}           # only the five Alice did not take
    assert "claimed 5 first" in notes(bob)
    await real_pull()
    assert not set(bob.claimed[BENCH]) & set(alice.claimed[BENCH])


def test_the_protocol_is_locked_by_the_owner_and_any_change_shows(tmp_path, ident):
    from harbor_master.ledger import protocol
    d = tmp_path / "index"
    d.mkdir()
    (d / ledger.CONTRIBUTORS).write_text(
        f'[[contributor]]\nname="T"\ngithub="{ident.github}"\nkey="{ident.public_key}"\nrole="owner"\n')
    cfg = config_mod.Config()
    ledger.Shard(d, protocol.BENCH, ident, "lock-1", cfg.protocol).append("protocol", settings=cfg.protocol_settings())
    lock = protocol.current(ledger.Ledger(d).events)
    assert lock["protocol"] == cfg.protocol and protocol.differences(lock["settings"], cfg.protocol_settings()) == []

    changed = config_mod.Config()
    changed.difficulty.max_agent_hours = 8                     # someone edits protocol.toml during the curation
    changed.tiers.worker.model = "another-model"
    assert changed.protocol != cfg.protocol
    diff = protocol.differences(lock["settings"], changed.protocol_settings())
    assert any(line.startswith("difficulty.max_agent_hours: locked 4.0, here 8") for line in diff)
    assert any(line.startswith("tiers.worker.model") for line in diff) and len(diff) == 2
    moved = config_mod.Config()
    moved.run.cells = 32                                       # how this machine runs is not part of it
    assert moved.protocol == cfg.protocol

    # A record made under settings that were never locked fails verification.
    time.sleep(0.01)
    ledger.Shard(d, "x", ident, "r1", changed.protocol).append(
        "trial", "x/t1", "dig", purpose="difficulty", agent="a", model="m", trial_index=0, passed=False)
    assert any("never locked" in p for p in ledger.verify(d))


def test_only_the_owner_may_lock(tmp_path, ident):
    from harbor_master.ledger import protocol
    d = tmp_path / "index"
    d.mkdir()
    (d / ledger.CONTRIBUTORS).write_text(
        f'[[contributor]]\nname="T"\ngithub="{ident.github}"\nkey="{ident.public_key}"\n')
    cfg = config_mod.Config()
    ledger.Shard(d, protocol.BENCH, ident, "lock-1", cfg.protocol).append("protocol", settings=cfg.protocol_settings())
    assert any("not the owner" in p for p in ledger.verify(d))


def test_the_skills_are_part_of_the_protocol():
    from harbor_master import skills
    assert len(skills.digest()) == 16 and config_mod.Config().protocol_settings()["skills"] == skills.digest()


async def test_a_run_shares_its_records_and_never_its_edits_to_the_index(world):
    session, act_as = world
    bob_ws = act_as("bob")["ws"]
    assert (bob_ws / config_mod.PROTOCOL_NAME).exists() and (bob_ws / ledger.CONTRIBUTORS).exists()   # joining brought the index
    (bob_ws / ledger.CONTRIBUTORS).write_text((bob_ws / ledger.CONTRIBUTORS).read_text().replace('github = "bob"', 'github = "bob"\nrole = "owner"'))
    (bob_ws / "roster.toml").write_text('[[config]]\nname = "mine"\nagent = "a"\nmodel = "m"\n')
    bob = session("bob", "b1")
    await shared.run_shared(bob, bob.benches[BENCH])                # Bob claims and pushes

    alice = session("alice", "a1")
    await alice.pull()
    alice_ws = act_as("alice")["ws"]
    assert any(e["runner"] == "bob" for e in alice.ledger.events)   # his records arrived
    assert 'role = "owner"' not in (alice_ws / ledger.CONTRIBUTORS).read_text().split("Bob")[1]   # his edit did not
    assert not (alice_ws / "roster.toml").exists()
    tracked = subprocess.run(["git", "ls-files"], cwd=alice_ws, capture_output=True, text=True).stdout.split()
    assert ".hm/tasks/registry.json" not in tracked and all(
        t.startswith("ledger/") or t in ("protocol.toml", "contributors.toml", ".gitignore") for t in tracked)
