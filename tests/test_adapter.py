"""A benchmark in another format: the owner builds an adapter, it is checked and locked, and everyone gets the same tasks."""
import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from harbor_master import ledger, tasks
from harbor_master.cli import app
from harbor_master.cli.common import check_lock
from harbor_master.core import config as config_mod
from harbor_master.core import identity
from harbor_master.interact import guide
from harbor_master.tasks import adapter

runner = CliRunner()
GOOD = '''"""Turns the toy benchmark's questions.jsonl into Harbor tasks."""
import json
from pathlib import Path

TEMPLATE = Path(__file__).parent / "task-template"


class Adapter:
    def __init__(self, output_dir, source_dir, limit=None, overwrite=False, task_ids=None, **kwargs):
        self.output_dir, self.source_dir, self.limit, self.task_ids = output_dir, source_dir, limit, task_ids

    def run(self):
        rows = [json.loads(line) for line in (self.source_dir / "questions.jsonl").read_text().splitlines() if line.strip()]
        rows = sorted(rows, key=lambda r: r["id"])
        if self.task_ids:
            rows = [r for r in rows if r["id"] in self.task_ids]
        for row in rows[:self.limit]:
            d = self.output_dir / row["id"]
            for sub in ("environment", "solution", "tests"):
                (d / sub).mkdir(parents=True, exist_ok=True)
            fill = lambda name: (TEMPLATE / name).read_text().replace("__ID__", row["id"]).replace("__ASK__", row["ask"]).replace("__ANSWER__", row["answer"])
            for name in ("task.toml", "instruction.md", "environment/Dockerfile", "solution/solve.sh", "tests/test.sh"):
                (d / name).write_text(fill(name))
'''
TEMPLATE = {
    "task.toml": '[task]\nname = "toy/__ID__"\n\n[metadata]\nfield = "arithmetic"\n\n[verifier]\ntimeout_sec = 60.0\n\n[agent]\ntimeout_sec = 600.0\n',
    "instruction.md": "__ASK__\n\nWrite the answer, and nothing else, to /app/answer.txt.\n",
    "environment/Dockerfile": "FROM python:3.12-slim\nWORKDIR /app\n",
    "solution/solve.sh": "#!/bin/bash\necho '__ANSWER__' > /app/answer.txt\n",
    "tests/test.sh": "#!/bin/bash\nmkdir -p /logs/verifier\nif [ \"$(cat /app/answer.txt 2>/dev/null)\" = '__ANSWER__' ]; then echo 1 > /logs/verifier/reward.txt; "
                     "else echo 0 > /logs/verifier/reward.txt; fi\n",
}
ENTRY = {"id": "toy", "name": "Toy Arithmetic", "description": "Sums of small numbers, one to a task.", "paper": "none",
         "version": "1.0", "released": "2026-09-01", "github": "https://example.org/toy", "ref": "v1.0",
         "commit": "0" * 40, "tasks": ".", "task_count": 3, "format": "jsonl", "license": "MIT", "fields": ["mathematical sciences"]}


def passes(ws: Path) -> bool:
    """Whether someone signed that the adapter in the index, as it is now, passed its checks."""
    from harbor_master.engine import prepare
    return prepare.approved(ledger.Ledger(ws).events, adapter.digest(ws / "adapters" / "toy"))


def hm(*args: object) -> str:
    result = runner.invoke(app, [str(a) for a in args])
    return f"{result.exit_code} {' '.join(result.output.split())}"


def write_good(folder: Path, code: str = GOOD) -> Path:
    """A finished adapter for the toy benchmark, written by hand."""
    pkg = adapter.scaffold(folder, {"id": "toy", "name": "Toy Arithmetic"})
    (pkg / "adapter.py").write_text(code)
    for name, text in TEMPLATE.items():
        (pkg / "task-template" / name).write_text(text)
    return folder


@pytest.fixture
def toy(tmp_path, monkeypatch):
    """An index whose owner listed a benchmark that is a JSONL file, not Harbor tasks."""
    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / "config"))
    identity.create("Ada", "Some University", "ada@example.org", "ada")
    source = tmp_path / "toy-source"
    source.mkdir()
    (source / "questions.jsonl").write_text("".join(json.dumps({"id": f"q{i}", "ask": f"What is {i} + {i}?", "answer": str(2 * i)}) + "\n"
                                                  for i in (3, 1, 2)))
    monkeypatch.setattr(tasks.supported, "fetch", lambda entry, sources: source)       # no network: the source is here
    ws = tmp_path / "index"
    assert hm("index", "new", "demo", ws).startswith("0 ")
    ledger.benchmarks.add(ws, identity.load(), config_mod.load(ws).protocol, ENTRY)
    return ws, source, tmp_path


def test_the_owner_brings_an_adapter_and_everyone_gets_the_same_tasks(toy):
    ws, source, tmp = toy
    assert hm("submit", "toy", "--no-sandbox", "-w", ws).startswith("1 toy is not in Harbor format, and this index has no adapter")
    state = guide.look(ws)
    assert state.to_adapt == ["toy"] and state.real == ["toy"] and "the run fetches it" in guide.text(state)

    made = hm("adapt", "toy", "--from", write_good(tmp / "by-hand"), "--oracle", 0, "--no-sandbox", "-w", ws)
    assert made.startswith("0 ") and "passes: 3 tasks, the same on a second run" in made
    assert (ws / "adapters" / "toy" / "src" / "toy" / "adapter.py").read_text() == GOOD
    record = next(e for e in ledger.Ledger(ws).events if e.get("stage") == "adapt")
    assert (record["outcome"], record["tier"], record["value"]) == ("pass", "human", "3 tasks")
    assert record["digest"] == adapter.digest(ws / "adapters" / "toy") and guide.look(ws).to_adapt == []

    assert passes(ws) and ledger.protocol.current(ledger.Ledger(ws).events) is None       # the signed check vouches for it: no lock

    done = hm("submit", "toy", "--no-sandbox", "-w", ws)
    assert done.startswith("0 ") and "3 tasks" in done and "written by the index's adapter" in done
    info = tasks.read_registry(ws)["toy"]
    found = tasks.load_benchmark(Path(info["path"]), "toy")
    assert [t.name for t in found] == ["q1", "q2", "q3"] and info["adapter"] == adapter.digest(ws / "adapters" / "toy")
    assert found[0].instruction.startswith("What is 1 + 1?") and all(t.has_solution and t.has_tests for t in found)
    first = adapter.digests(Path(info["path"]))
    hm("submit", "toy", "--no-sandbox", "-w", ws)
    assert adapter.digests(Path(info["path"])) == first                       # the same tasks, whoever runs it and whenever

    code = ws / "adapters" / "toy" / "src" / "toy" / "adapter.py"             # a changed adapter is one nobody vouched for:
    code.write_text(code.read_text().replace('sorted(rows, key=lambda r: r["id"])', "rows"))       # the next run checks it again
    assert not passes(ws)


def test_an_adapter_that_cannot_be_trusted_is_reported(toy):
    ws, source, tmp = toy
    work = tmp / "work"
    check = lambda folder, n=3: adapter.check(folder, source, work, n, sandbox=False)[0]  # noqa: E731
    assert check(write_good(tmp / "a")) == []
    assert "the benchmark list says 5" in check(tmp / "a", 5)[0]
    stamped = GOOD.replace('fill(name))', 'fill(name) + f"# {__import__(\'time\').time_ns()}\\n")')
    assert "a second run did not write the same tasks" in check(write_good(tmp / "b", stamped))[0]
    assert "imports requests: an adapter may use the standard library only" in " ".join(check(write_good(tmp / "c", "import requests\n" + GOOD)))
    assert "it failed when run" in check(write_good(tmp / "d", GOOD.replace("questions.jsonl", "missing.jsonl")))[0]
    assert "no tests/test.sh" in check(write_good(tmp / "e", GOOD.replace(', "tests/test.sh"):', "):")))[0]
    assert check(adapter.scaffold(tmp / "f", {"id": "toy"}).parents[1])[0].startswith("it failed when run")      # the stub
    (tmp / "a" / "src" / "toy" / "main.py").write_text("print('mine')\n")
    assert "main.py does not take --source-dir" in check(tmp / "a")
    assert adapter.structure(tmp / "nothing") == ["no src/<package>/adapter.py"]


def broken(code: str = GOOD) -> str:
    return code.replace(', "tests/test.sh"):', "):")


def reply(code: str) -> dict:
    files = [{"path": "adapter.py", "content": code}, *({"path": f"task-template/{n}", "content": t} for n, t in TEMPLATE.items()),
             {"path": "../escape.py", "content": "x"}, {"path": "main.py", "content": "print('not the model\\'s to write')"}]
    return {"files": files, "task_count": 3, "notes": "Carried over as it is."}


def test_the_models_write_the_adapter_the_stronger_one_when_the_first_fails(toy, monkeypatch):
    ws, source, tmp = toy
    from harbor_master.backends import adapter as writer
    asked: list[tuple[str, str]] = []

    async def stand_in(cfg, entry, source_dir, feedback="", tier="worker"):
        """The worker tier never gets it right. The frontier tier does."""
        asked.append((tier, feedback))
        return reply(GOOD if tier == "frontier" else broken()), 0.25, 1000, "adapt/write-adapter@test"

    monkeypatch.setattr(writer, "write_adapter", stand_in)
    made = hm("adapt", "toy", "--oracle", 0, "--no-sandbox", "-w", ws)
    assert made.startswith("0 ") and "passes: 3 tasks" in made and "cost $0.75" in made and "Carried over as it is." in made
    assert [tier for tier, _ in asked] == ["worker", "worker", "frontier"]                   # two tries, then the stronger model
    assert asked[0][1] == "" and "no tests/test.sh" in asked[1][1] and "no tests/test.sh" in asked[2][1]     # each told what failed
    pkg = ws / "adapters" / "toy" / "src" / "toy"
    assert not (ws / "adapters" / "toy" / "src" / "escape.py").exists() and "--source-dir" in (pkg / "main.py").read_text()
    record = next(e for e in ledger.Ledger(ws).events if e.get("stage") == "adapt")
    assert (record["tier"], record["cost"], record["rounds"], record["skill"]) == ("frontier", 0.75, 3, "adapt/write-adapter@test")

    assert "passes" in hm("adapt", "toy", "--oracle", 0, "--no-sandbox", "-w", ws) and len(asked) == 3   # one that is there is checked, not rewritten
    prompt = writer.about({"id": "toy", "name": "Toy", "format": "jsonl", "email": "x"}) + writer.layout(source)
    assert "format: jsonl" in prompt and "questions.jsonl" in prompt and "email" not in prompt


def entry_of(ws: Path) -> dict:
    return tasks.supported.find(ws, "toy")


def run_of(ws: Path, run_id: str, who=None, **more):
    """A run of the listed, unfetched toy benchmark. A simulated backend stands in for the real one."""
    from harbor_master.engine import Session
    cfg = config_mod.load(ws)
    cfg.run.sandbox = "none"
    entry = entry_of(ws)
    info = {"path": "", "field": "mathematical sciences", "supported": "toy", "entry": entry}
    return Session(ws, cfg, who or identity.load(), {"toy": info}, speed=0, run_id=run_id, **{"simulate": True, "as_real": True, **more})


@pytest.fixture
def locked(toy):
    """The toy index, locked, with nothing asked of the lead during a run."""
    ws, source, tmp = toy
    protocol = ws / "protocol.toml"
    protocol.write_text(protocol.read_text().replace("require_brief = true ", "require_brief = false").replace("require_signoff = true ", "require_signoff = false"))
    assert "Locked protocol" in hm("protocol", "lock", "-w", ws)
    return ws, source, tmp


async def test_a_run_fetches_adapts_and_locks_with_no_one_asked(locked, monkeypatch):
    ws, source, tmp = locked
    from harbor_master.backends import adapter as writer
    asked: list[str] = []

    async def stand_in(cfg, entry, source_dir, feedback="", tier="worker"):
        asked.append(tier)
        return reply(GOOD if tier == "frontier" else broken()), 0.25, 1000, "adapt/write-adapter@test"

    monkeypatch.setattr(writer, "write_adapter", stand_in)
    s = run_of(ws, "r1")
    await s.run()                                                              # nothing was fetched or adapted before this
    bench = s.benches["toy"]
    assert bench.phase == "ready" and len(bench.tasks) == 3 and asked == ["worker", "worker", "frontier"]
    assert not any(r.blocking for r in s.requests)                             # nobody was asked anything on the way
    assert s.cost_by_tier["worker"] >= 0.5 and s.cost_by_tier["frontier"] >= 0.25           # the models' work is in the run's cost
    assert passes(ws) and len(ledger.protocol.locks(ledger.Ledger(ws).events)) == 1        # vouched for by its record, with no new lock
    assert ledger.verify(ws, max_per_benchmark=8) == []
    done = next(e for e in s.ledger.events if e.get("stage") == "adapt")
    assert (done["outcome"], done["tier"], done["rounds"]) == ("pass", "frontier", 3)
    assert tasks.read_registry(ws)["toy"]["adapter"] == adapter.digest(ws / "adapters" / "toy")

    again = run_of(ws, "r2")                                                   # the next run uses the locked adapter as it is
    await again.run()
    assert again.benches["toy"].phase == "ready" and len(asked) == 3
    tried = run_of(ws, "r3", as_real=False)                                    # and so does a simulated one
    await tried.run()
    assert tried.benches["toy"].phase == "ready" and len(asked) == 3


async def test_a_person_is_asked_only_when_the_models_cannot(locked, monkeypatch):
    import asyncio
    from harbor_master import interact
    from harbor_master.backends import adapter as writer
    from harbor_master.interact import Command
    ws, source, tmp = locked
    asked: list[str] = []

    async def never(cfg, entry, source_dir, feedback="", tier="worker"):
        asked.append(tier)
        return reply(broken()), 0.25, 1000, "adapt/write-adapter@test"

    monkeypatch.setattr(writer, "write_adapter", never)

    async def until_asked(s):
        run = asyncio.create_task(s.run())
        for _ in range(500):
            await asyncio.sleep(0.01)
            if s.requests or run.done():
                break
        return run

    trying = run_of(ws, "s0", as_real=False)                                   # a simulated run builds nothing, and says why
    await trying.run()
    assert trying.benches["toy"].phase == "skipped" and "has no adapter yet" in trying.benches["toy"].detail and not asked

    s = run_of(ws, "r1")
    run = await until_asked(s)
    ask = s.requests[0]
    assert ask.kind == "adapter" and ask.blocking and "does not pass after 3 rounds" in ask.text and "no tests/test.sh" in ask.text
    assert asked == ["worker", "worker", "frontier"] and s.benches["toy"].phase == "review"
    assert "/recheck toy" in guide.session_tip(s) and "/abandon toy" in interact.how_to_answer(ask)
    assert interact.complete(s, "/re")[0].startswith("/recheck") and interact.complete(s, "/recheck ") == ["/recheck toy"]

    assert "again" in interact.execute(s, Command("recheck", ["toy"]))           # no fix was made: the frontier model tries once more
    for _ in range(500):
        await asyncio.sleep(0.01)
        if len(asked) == 4 and s.requests:
            break
    assert asked[-1] == "frontier" and "after 4 rounds" in s.requests[0].text

    write_good(ws / "adapters" / "toy")                                        # the person fixes it by hand
    interact.execute(s, Command("recheck", ["toy"]))
    await asyncio.wait_for(run, 10)
    assert s.benches["toy"].phase == "ready" and len(asked) == 4               # checked as it is: no model was asked again
    assert passes(ws)


async def test_an_abandoned_benchmark_is_refused_to_everyone_until_its_owner_reopens_it(locked, monkeypatch):
    import asyncio
    from harbor_master import interact
    from harbor_master.backends import adapter as writer
    from harbor_master.interact import Command
    ws, source, tmp = locked
    asked: list[str] = []

    async def never(cfg, entry, source_dir, feedback="", tier="worker"):
        asked.append(tier)
        return reply(broken()), 0.25, 1000, "adapt/write-adapter@test"

    monkeypatch.setattr(writer, "write_adapter", never)
    unattended = run_of(ws, "r0", no_input=True)                               # with nothing to ask, a failing adapter waits for a person
    await unattended.run()
    assert unattended.benches["toy"].phase == "skipped" and "nothing could ask you" in unattended.benches["toy"].detail
    assert ledger.abandoned(ledger.Ledger(ws).events) == {}                    # and that is not giving it up

    s = run_of(ws, "r1")                                                       # the adapter is there now: it is checked, not rewritten
    run = asyncio.create_task(s.run())
    for _ in range(500):
        await asyncio.sleep(0.01)
        if s.requests:
            break
    assert len(asked) == 3 and "does not pass" in s.requests[0].text
    assert interact.needs_confirmation(interact.interpret(s, "/abandon toy"))                 # giving a benchmark up is confirmed
    assert "abandoned" in interact.execute(s, Command("abandon", ["toy"]))
    await asyncio.wait_for(run, 10)
    assert s.benches["toy"].phase == "skipped" and "Only /adapt toy reopens it" in s.benches["toy"].detail
    dropped = ledger.abandoned(ledger.Ledger(ws).events)["toy"]
    assert (dropped["outcome"], dropped["value"], dropped["tier"]) == ("drop", "abandoned", "human")
    assert not passes(ws)

    (ws / "adapters" / "toy" / "README.md").write_text("touched\n")           # a changed file does not reopen it
    monkeypatch.setattr(tasks.supported, "fetch", lambda entry, sources: pytest.fail("an abandoned benchmark is not even fetched"))
    for again in (run_of(ws, "r2"), run_of(ws, "r3", as_real=False)):          # the owner's next run, and a simulated one
        await again.run()
        assert again.benches["toy"].phase == "skipped" and "abandoned by @ada" in again.benches["toy"].detail
        assert "/adapt toy" in again.benches["toy"].detail and "no tests/test.sh" in again.benches["toy"].detail
    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp / "config-bob"))               # and anyone else's
    bob = identity.create("Bob", "Other University", "bob@example.org", "bob")
    with (ws / "contributors.toml").open("a") as f:
        f.write(f'\n[[contributor]]\nname = "Bob"\ngithub = "bob"\nkey = "{bob.public_key}"\nfields = ["*"]\n')
    other = run_of(ws, "r4", who=bob)
    await other.run()
    assert other.benches["toy"].phase == "skipped" and "abandoned by @ada" in other.benches["toy"].detail and len(asked) == 3

    named = hm("run", "toy", "--simulate", "--plain", "-w", ws)                # asked for by name, it is refused outright
    assert named.startswith("1 Refused: toy was abandoned by @ada") and "toy was abandoned by @ada" in hm("benchmarks", "-w", ws)
    state = guide.look(ws)
    assert state.abandoned == ["toy"] and state.real == [] and guide.next_step(state) == "A benchmark" and "toy (abandoned)" in guide.text(state)

    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp / "config"))                   # the owner reopens it with an adapter that passes
    monkeypatch.setattr(tasks.supported, "fetch", lambda entry, sources: source)
    reopened = await asyncio.to_thread(hm, "adapt", "toy", "--from", write_good(tmp / "by-hand"), "--oracle", 0, "--no-sandbox", "-w", ws)
    assert "toy was abandoned" in reopened and "passes: 3 tasks" in reopened and ledger.abandoned(ledger.Ledger(ws).events) == {}
    back = run_of(ws, "r5")
    await back.run()
    assert back.benches["toy"].phase == "ready" and len(asked) == 3            # checked, locked and used: no model was asked
    assert passes(ws)


async def test_a_run_fetches_a_listed_benchmark_that_is_in_harbor_format(locked, monkeypatch):
    from conftest import make_benchmark
    ws, source, tmp = locked
    made = make_benchmark(tmp / "fake", "plain", 6)
    monkeypatch.setattr(tasks.supported, "fetch", lambda entry, sources: made)
    from harbor_master.engine import Session
    entry = {**entry_of(ws), "id": "plain", "format": "harbor", "task_count": 6}
    s = Session(ws, config_mod.load(ws), identity.load(), {"plain": {"path": "", "field": "chemistry", "supported": "plain", "entry": entry}},
                simulate=True, as_real=True, speed=0, run_id="r1")
    await s.run()
    assert s.benches["plain"].phase == "ready" and len(s.benches["plain"].tasks) == 6
    assert tasks.read_registry(ws)["plain"]["path"] == str(made.resolve())     # registered, so the next run does not fetch


def test_plain_hm_run_takes_what_the_index_lists(locked):
    ws, source, tmp = locked
    settings = ws / ".hm" / "hm.toml"
    settings.write_text(settings.read_text().replace('sandbox = "docker"', 'sandbox = "none"  '))      # no container in a test
    out = hm("run", "--simulate", "--plain", "--speed", 0, "-w", ws)           # nothing was submitted first
    assert "toy" in out and "has no adapter yet" in out
    assert hm("run", "nothing", "--simulate", "--plain", "-w", ws).startswith("1 nothing is not on this index's list of benchmarks")
    write_good(ws / "adapters" / "toy")
    out = hm("run", "--simulate", "--plain", "--speed", 0, "--no-input", "-w", ws)        # with an adapter there, a simulated run uses it
    assert out.startswith("0 ") and "3" in out and tasks.read_registry(ws)["toy"]["path"].endswith(".hm/adapted/toy/tasks")
