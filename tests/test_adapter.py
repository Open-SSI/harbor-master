"""A benchmark in another format: a run writes its adapter, or a person brings one, it is checked, and everyone gets the same tasks."""
import asyncio
import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from harbor_master import ledger, tasks
from harbor_master.backends import adapter as writer
from conftest import call
from harbor_master.cli import app
from harbor_master.cli.setup import index_new
from harbor_master.core import config as config_mod
from harbor_master.core import identity
from harbor_master.engine import Session, prepare
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
         "commit": "0" * 40, "tasks": ".", "task_count": 3, "format": "jsonl", "license": "MIT"}
BROKEN = GOOD.replace(', "tests/test.sh"):', "):")      # an adapter that writes no tests/test.sh


def pkg(ws: Path) -> Path:
    return ws / "adapters" / "toy"


def passes(ws: Path) -> bool:
    """Whether someone signed that the adapter in the index, as it is now, passed its checks."""
    return prepare.approved(ledger.Ledger(ws).events, adapter.digest(pkg(ws)))


def hm(*args: object) -> str:
    result = runner.invoke(app, [str(a) for a in args])
    return f"{result.exit_code} {' '.join(result.output.split())}"


def models(monkeypatch, fixes: bool = True, worker_thinks: bool = True) -> tuple[list[str], list[str]]:
    """Stand-ins for the models that write an adapter: the tiers asked, and what each was told had failed.
    The worker tier never gets it right. The decision model judges it can be adapted unless `worker_thinks` is off.
    The frontier tier gets it right, unless `fixes` is off."""
    asked, told = [], []

    async def write_adapter(cfg, entry, source_dir, feedback="", tier="worker"):
        asked.append(tier)
        told.append(feedback)
        files = [{"path": "adapter.py", "content": GOOD if fixes and tier == "frontier" else BROKEN},
                 *({"path": f"task-template/{n}", "content": t} for n, t in TEMPLATE.items()),
                 {"path": "../escape.py", "content": "x"}, {"path": "main.py", "content": "print('not the model\\'s to write')"}]
        return {"files": files, "task_count": 3, "notes": "Carried over as it is. Its grading needs a person."}, 0.25, 1000, "adapt/write-adapter@test"

    async def judge(cfg, entry, source_dir, problems, notes):       # Jev's typed choice, with its probability
        asked.append("decision")
        return ("can_be_adapted", 0.8, 0.001, 500, "adapt/judge@test") if worker_thinks else ("cannot_be_adapted", 0.9, 0.001, 500, "adapt/judge@test")

    monkeypatch.setattr(writer, "write_adapter", write_adapter)
    monkeypatch.setattr(writer, "judge", judge)
    return asked, told


def run_of(ws: Path, run_id: str, who=None, **more):
    """A run of the listed, unfetched toy benchmark. A simulated backend stands in for the real one."""
    cfg = config_mod.load(ws)
    cfg.run.sandbox = "none"
    info = {"path": "", "supported": "toy", "entry": tasks.supported.find(ws, "toy")}
    return Session(ws, cfg, who or identity.load(), {"toy": info}, speed=0, run_id=run_id, **{"simulate": True, "as_real": True, **more})


async def ran(ws: Path, run_id: str, **more):
    s = run_of(ws, run_id, **more)
    await s.run()
    return s


async def wait(until) -> None:
    for _ in range(500):
        await asyncio.sleep(0.01)
        if until():
            return


async def start(s) -> asyncio.Task:
    """A run under way, waited for until it asks something or ends."""
    run = asyncio.create_task(s.run())
    await wait(lambda: s.requests or run.done())
    return run


@pytest.fixture
def toy(tmp_path, monkeypatch):
    """An index whose owner listed a benchmark that is a JSONL file, not Harbor tasks."""
    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / "config"))
    identity.create("Ada", "Some University", "ada@example.org", "ada")
    source, ws = tmp_path / "toy-source", tmp_path / "index"
    source.mkdir()
    (source / "questions.jsonl").write_text("".join(json.dumps({"id": f"q{i}", "ask": f"What is {i} + {i}?", "answer": str(2 * i)}) + "\n"
                                                  for i in (3, 1, 2)))
    monkeypatch.setattr(tasks.supported, "fetch", lambda entry, sources: source)       # no network: the source is here
    assert call(index_new, "demo", ws, "", "", str(tmp_path / "store")).startswith("0 ")
    ledger.benchmarks.add(ws, identity.load(), config_mod.load(ws).protocol, ENTRY)
    return ws, source, tmp_path


@pytest.fixture
def quiet(toy):
    """The toy index, with nothing asked of the lead during a run."""
    protocol = toy[0] / "protocol.toml"
    protocol.write_text(protocol.read_text().replace("require_brief = true ", "require_brief = false").replace("require_signoff = true ", "require_signoff = false"))
    return toy


async def test_a_run_fetches_adapts_and_locks_with_no_one_asked(quiet, monkeypatch):
    ws, source, tmp = quiet
    asked, told = models(monkeypatch)
    s = await ran(ws, "r1")                                                    # nothing was fetched or adapted before this
    bench = s.benches["toy"]
    assert bench.phase == "ready" and len(bench.tasks) == 3 and asked == ["worker", "worker", "decision", "frontier"]   # two tries, then the stronger model
    assert told[0] == "" and "no tests/test.sh" in told[1] and "no tests/test.sh" in told[2]     # each told what failed
    assert not (pkg(ws) / "src" / "escape.py").exists() and "--source-dir" in (pkg(ws) / "src" / "toy" / "main.py").read_text()
    assert not any(r.blocking for r in s.requests)                             # nobody was asked anything on the way
    assert s.cost_by_tier["worker"] >= 0.5 and s.cost_by_tier["frontier"] >= 0.25           # the models' work is in the run's cost
    assert passes(ws) and not any(e["type"] == "protocol" for e in ledger.Ledger(ws).events)        # vouched for by its record
    assert ledger.verify(ws) == []
    done = next(e for e in s.ledger.events if e.get("stage") == "adapt" and e.get("gate") == "adapter")
    assert (done["outcome"], done["tier"], done["rounds"], done["skill"]) == ("pass", "frontier", 3, "adapt/write-adapter@test")
    prompt = writer.about({"id": "toy", "name": "Toy", "format": "jsonl", "email": "x"}) + writer.layout(source)
    assert "format: jsonl" in prompt and "questions.jsonl" in prompt and "email" not in prompt
    assert tasks.read_registry(ws)["toy"]["adapter"] == adapter.digest(pkg(ws))
    again = await ran(ws, "r2")                                                # the next run uses the locked adapter as it is
    assert again.benches["toy"].phase == "ready" and len(asked) == 4
    tried = await ran(ws, "r3", as_real=False)                                 # and so does a simulated one
    assert tried.benches["toy"].phase == "ready" and len(asked) == 4


async def test_a_benchmark_no_model_can_adapt_is_dropped_for_good_and_cannot_be_added_again(quiet, monkeypatch):
    ws, source, tmp = quiet
    asked, _ = models(monkeypatch, worker_thinks=False)
    s = run_of(ws, "r1")
    run = await start(s)
    assert s.requests[0].kind == "rejected" and s.requests[0].choices == ["ok"]   # the decision model is sure: the person only confirms
    s.answer_request(s.requests[0], "ok")
    await asyncio.wait_for(run, 10)
    assert s.benches["toy"].phase == "skipped" and "judges it cannot be adapted" in s.benches["toy"].detail and asked == ["worker", "worker", "decision"]
    dropped = ledger.abandoned(ledger.Ledger(ws).events)["toy"]
    assert (dropped["outcome"], dropped["value"]) == ("drop", "abandoned") and "judged not adaptable (90%)" in dropped["reason"]

    monkeypatch.setattr(tasks.supported, "fetch", lambda entry, sources: pytest.fail("a dropped benchmark is not even fetched"))
    for again in (run_of(ws, "r2"), run_of(ws, "r3", as_real=False)):          # the next run, and a simulated one
        await again.run()
        assert again.benches["toy"].phase == "skipped" and "dropped by @ada" in again.benches["toy"].detail
        assert "cannot be added again" in again.benches["toy"].detail
    named = hm("curate", "toy", "--simulate", "--plain", "-w", ws)                # asked for by name, it is refused outright
    assert named.startswith("1 Refused: toy was dropped by @ada") and "dropped" in hm("benchmarks", "-w", ws)
    assert "stays on the list as dropped" in hm("benchmarks", "remove", "toy", "-w", ws)       # it stays on the list
    assert tasks.supported.find(ws, "toy") is not None
    import tomli_w
    ws.joinpath("benchmarks.toml").write_text(tomli_w.dumps({"benchmark": []}))  # and even off the list, it is never added again
    from harbor_master.cli import curate as curate_cli
    with pytest.raises(typer.Exit):
        curate_cli._clash(ws, "toy-again", ENTRY["github"] + ".git")
    assert list(ledger.abandoned(ledger.Ledger(ws).events)) == ["toy"]


async def test_a_listed_benchmark_its_checks_reject_costs_no_adapter(quiet, monkeypatch):
    ws, source, tmp = quiet
    asked, _ = models(monkeypatch)
    ledger.benchmarks.add(ws, identity.load(), config_mod.load(ws).protocol, {**ENTRY, "released": "2023-03-01"})
    s = await ran(ws, "r1", no_input=True)                  # nobody to confirm: left out of this run, and no model asked
    assert s.benches["toy"].skip == "checks_failed" and asked == []
