"""What a run records for the appendix: model calls by stage and tier, skipped benchmarks, and the tables made from them."""
import csv

from typer.testing import CliRunner

from harbor_master import interact, ledger, tasks
from harbor_master.cli import app
from harbor_master.core import config as config_mod
from harbor_master.engine import Session
from harbor_master.interact import Command
from harbor_master.ledger import stats

BENCH = "demo-bench"


def run_of(workspace, ident, run_id, **settings):
    cfg = config_mod.load(workspace)
    for name, value in settings.items():
        setattr(cfg.select, name, value)
    return Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=True, as_real=True, speed=0, no_input=True, run_id=run_id)


async def test_every_model_call_is_recorded_under_its_stage_and_tier(workspace, ident):
    s = run_of(workspace, ident, "r1")
    await s.run()
    events = ledger.Ledger(workspace).events
    rows = [r for e in events if e["type"] == "usage" for r in e["rows"]]
    by_tier = lambda key: {t: sum(r[key] for r in rows if r["tier"] == t) for t in {r["tier"] for r in rows}}  # noqa: E731
    assert by_tier("calls") == dict(s.calls)                                   # nothing counted twice, nothing missed
    assert all(abs(by_tier("spent")[t] - s.cost_by_tier[t]) < 1e-3 for t in s.calls)
    assert {"triage", "rank", "probe"} <= {r["stage"] for r in rows} and all(r["tokens"] > 0 and r["model"] for r in rows)
    assert all(e["benchmark"] == BENCH for e in events if e["type"] == "usage")
    assert ledger.verify(workspace) == []                                      # the new records are sound ones
    spent = sum(float(e.get("cost", 0) or 0) for e in events)
    assert abs(ledger.credits(events)[ident.github]["cost"] - spent) < 1e-6    # and are not added to anyone's cost a second time

    again = run_of(workspace, ident, "r2")                                     # a second run reuses every gate and trial.
    await again.run()                                                          # It ranks again, and the record shows exactly that
    later = [r for e in ledger.Ledger(workspace).events if e["type"] == "usage" and e["time"] > events[-1]["time"] for r in e["rows"]]
    assert {r["stage"] for r in later} == {"rank"} and sum(r["calls"] for r in later) == sum(again.calls.values())

    interact.execute(again, Command("accept", [again.ready[0].name, "clean"]))
    found = stats.tables(ledger.Ledger(workspace).events)
    bench = s.benches[BENCH]
    row = dict(zip(*[found["benchmarks"][0], found["benchmarks"][1][0]]))
    assert (row["submitted"], row["failed a gate"], row["not selected"], row["selected"]) == (24, bench.triaged_out, bench.not_selected, 20 - 0 if bench.selected else 0)
    assert row["accepted"] == 1 and row["report ready"] == bench.kept - 1 and row["dropped at difficulty"] + row["dropped at fix"] + row["dropped at verify"] \
        + row["dropped at probe"] + row["dropped at audit"] == bench.dropped
    total = dict(found["summary"][1])
    assert total["tasks accepted"] == 1 and total["cost per accepted task"] > 0
    assert total["decision tier calls"] == s.calls["decision"] + again.calls["decision"]              # both runs, added up
    assert abs(total["model tiers, total cost"] - sum(s.cost_by_tier.values()) - sum(again.cost_by_tier.values())) < 1e-2
    assert abs(total["runs through Harbor, cost"] - s.trial_cost) < 1e-2 and again.trial_cost == 0
    assert ["verdict: accept", 1] in found["decision_counts"][1] and found["decisions"][1][0][1] == ident.github
    assert {r[0] for r in found["models"][1]} >= {"triage", "rank"} and {r[0] for r in found["harbor_runs"][1]} >= {"oracle", "noop", "difficulty"}
    assert "select" in {r[1] for r in found["drops"][1]} and sum(r[3] for r in found["drops"][1]) == bench.triaged_out + bench.not_selected + bench.dropped
    assert sum(r[2] for r in found["gates"][1]) > 0 and len(found["tasks"][1]) == 24


async def test_a_skipped_benchmark_leaves_its_reason(workspace, ident):
    s = run_of(workspace, ident, "r1", require_brief=True)                     # nobody to give the brief
    await s.run()
    skipped = [e for e in ledger.Ledger(workspace).events if e["type"] == "skip"]
    assert len(skipped) == 1 and (skipped[0]["benchmark"], skipped[0]["value"]) == (BENCH, "no_brief") and "no brief" in skipped[0]["reason"]
    await run_of(workspace, ident, "r2", require_brief=True).run()             # the same reason again is not a new fact
    events = ledger.Ledger(workspace).events
    assert sum(e["type"] == "skip" for e in events) == 1 and ledger.verify(workspace) == []
    found = stats.tables(events)
    assert found["skips"][1][0][1:4] == [BENCH, ident.github, "no_brief"] and found["benchmarks"][1][0][-1] == "no_brief"
    assert dict(found["summary"][1])["times a benchmark was skipped"] == 1

    ledger.Shard(workspace, BENCH, ident, "brief", s.protocol).append("brief", text="Spectra matter.", examples=[], version=1)
    await run_of(workspace, ident, "r3", require_brief=True, require_signoff=True).run()      # another reason is
    assert [e["value"] for e in ledger.Ledger(workspace).events if e["type"] == "skip"] == ["no_brief", "selection_not_approved"]
    assert ["brief", 1] in stats.tables(ledger.Ledger(workspace).events)["decision_counts"][1]


def test_hm_stats_writes_a_report_and_its_tables_on_this_machine_only(workspace, ident):
    import asyncio
    asyncio.run(run_of(workspace, ident, "r1").run())
    out = CliRunner().invoke(app, ["stats", "-w", str(workspace)])
    folder = workspace / ".hm" / "stats"
    assert out.exit_code == 0 and "tasks submitted" in out.output and "this machine only" in " ".join(out.output.split())
    report = (folder / "stats.md").read_text()
    assert report.startswith("# Curation statistics") and "SIMULATED" not in report
    assert [line for line in report.splitlines() if line.startswith("## ")] == [
        "## 1. What happened to the tasks", "## 2. What it cost", "## 3. What people had to do", "## 4. Where these figures come from"]
    assert "**Model calls by stage**" in report and "| submitted | 24 | 100% |" in report and "`tables/tasks.csv`" in report
    assert sorted(p.stem for p in (folder / "tables").glob("*.csv")) == sorted([*stats.TABLES, "source"]) and not list(folder.glob("*.csv"))
    with (folder / "tables" / "source.csv").open() as f:
        source = dict(csv.reader(f))
    assert source["ledger checksum"] == ledger.digest(workspace) and source["ledger check"] == "sound"       # no repository holds these
    assert source["commit"].startswith("none holds these records") and source["pulled first"] == "no: this index is not shared"
    with (folder / "tables" / "models.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert set(rows[0]) == {"stage", "tier", "calls", "tokens", "cost"} and sum(int(r["calls"]) for r in rows) > 0
    assert (workspace / ".gitignore").read_text().startswith(".hm/")           # the folder it writes to is one git ignores

    sim = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, speed=0, no_input=True, run_id="s1")
    asyncio.run(sim.run())
    out = CliRunner().invoke(app, ["stats", "--simulated", "--out", str(workspace / "elsewhere"), "-w", str(workspace)])
    assert out.exit_code == 0 and "**SIMULATED.**" in (workspace / "elsewhere" / "stats.md").read_text()


def test_hm_stats_covers_everyone_and_says_which_state_of_the_ledger_it_read(workspace, ident, tmp_path):
    import asyncio
    import subprocess
    remote = tmp_path / "shared.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)], check=True)
    mine = ledger.Sync(workspace, ident.name, ident.github)
    mine.define("start")
    mine.connect(str(remote))
    other = tmp_path / "other-clone"                                           # a second machine on the same index
    ledger.Sync.clone(str(remote), other, ident.name, ident.github)
    from pathlib import Path
    tasks.register(other, BENCH, Path(tasks.read_registry(workspace)[BENCH]["path"]))
    there = run_of(other, ident, "r1")
    asyncio.run(there.run())                                                   # it curates and pushes. Nothing ran here
    assert ledger.Ledger(workspace).events == [] and len(ledger.Ledger(other).events) > 50

    hm = lambda *a: CliRunner().invoke(app, ["stats", *a, "-w", str(workspace)])  # noqa: E731
    stale = hm("--no-pull", "--out", str(tmp_path / "stale"))
    assert stale.exit_code == 0 and dict(csv.reader((tmp_path / "stale" / "tables" / "source.csv").open()))["records"] == "0"
    out = hm()                                                                 # by default it brings in what others pushed
    source = dict(csv.reader((workspace / ".hm" / "stats" / "tables" / "source.csv").open()))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True).stdout.strip()
    assert out.exit_code == 0 and source["pulled first"] == "yes" and int(source["records"]) == len(ledger.Ledger(other).events)
    assert source["commit"] == head == subprocess.run(["git", "rev-parse", "main"], cwd=remote, capture_output=True, text=True).stdout.strip()
    assert source["ledger checksum"] == ledger.digest(other) and source["commits not pushed"] == "0" and source["ledger check"] == "sound"
    assert source["made again with"] == f"git checkout {head[:12]} && hm stats --no-pull" and source["people with records"] == "1"
    report = (workspace / ".hm" / "stats" / "stats.md").read_text()
    assert "## 4. Where these figures come from" in report and head in report and str(there.calls["decision"]) in report.replace(",", "")
    tracked = subprocess.run(["git", "status", "--porcelain"], cwd=workspace, capture_output=True, text=True).stdout
    assert ".hm" not in tracked                                               # the report itself is never part of the index
