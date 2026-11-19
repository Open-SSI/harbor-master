"""The cheap filters, and the rule that the difficulty run happens once per task (DESIGN.md Section 4)."""
from collections import Counter

from harbor_master import ledger
from harbor_master.curation import leak, trivial


def _task(root, instruction="Fit the model and write the result to /app/out/fit.json.\n"):
    for sub in ("environment", "solution", "tests"):
        (root / sub).mkdir(parents=True)
    (root / "instruction.md").write_text(instruction)
    (root / "environment" / "Dockerfile").write_text("FROM python:3.12\nCOPY data /app/data\n")
    (root / "solution" / "solve.sh").write_text("#!/bin/bash\npython - <<'PY'\nimport json, statistics\n"
                                                "json.dump({'k': statistics.mean([1.0, 2.0, 4.5])}, open('/app/out/fit.json', 'w'))\nPY\n")
    (root / "tests" / "test_fit.py").write_text("import json\n\ndef test_fit():\n"
                                                "    assert abs(json.load(open('/app/out/fit.json'))['k'] - 2.5) < 1e-6\n")
    return root


def test_a_clean_task_has_no_leak(tmp_path):
    task = _task(tmp_path / "t")
    assert leak.find(task, (task / "instruction.md").read_text()) == (leak.NONE, [])


def test_a_solution_or_test_copied_into_the_image_is_a_leak(tmp_path):
    a = _task(tmp_path / "a")
    (a / "environment" / "helper.sh").write_bytes((a / "solution" / "solve.sh").read_bytes())
    kind, where = leak.find(a, "")
    assert kind == "solution_in_image" and "environment/helper.sh" in where[0]

    b = _task(tmp_path / "b")
    (b / "environment" / "check.py").write_bytes((b / "tests" / "test_fit.py").read_bytes())
    assert leak.find(b, "")[0] == "tests_in_image"

    c = _task(tmp_path / "c")
    (c / "environment" / "Dockerfile").write_text("FROM python:3.12\nCOPY ../solution /opt/ref\n")
    assert leak.find(c, "")[0] == "solution_in_image"

    d = _task(tmp_path / "d")       # a data file the tests share with the environment is an input, not a leak
    (d / "tests" / "input.csv").write_text("x,y\n" + "1,2\n" * 40)
    (d / "environment" / "input.csv").write_text("x,y\n" + "1,2\n" * 40)
    assert leak.find(d, "")[0] == leak.NONE


def test_an_instruction_that_quotes_the_solution_is_a_leak(tmp_path):
    task = _task(tmp_path / "t")
    quoted = "Do this:\n" + (task / "solution" / "solve.sh").read_text()
    (task / "solution" / "notes.py").write_text("values = [1.0, 2.0, 4.5]  # the measurements\nprint(sum(values) / len(values) + 0.0)\n"
                                                "assert len(values) == 3, 'three values are expected'\n")
    assert leak.find(task, quoted + (task / "solution" / "notes.py").read_text())[0] == "solution_in_instruction"


def test_trivial_submissions_are_written_for_the_files_a_task_names(tmp_path):
    task = _task(tmp_path / "t", "Read /app/data/in.json, fit the model and write /app/out/fit.json. See notes.txt.\n")
    (task / "environment" / "in.json").write_text("{}")
    text = (task / "instruction.md").read_text()
    assert trivial.outputs(task, text) == ["/app/out/fit.json"]       # not the input, and not a file the tests never name
    tries = trivial.submissions(task, text)
    assert [m["id"] for m in tries] == ["trivial-empty", "trivial-placeholder", "trivial-copy"]
    assert all(m["kind"] == "wrong" and "/app/out/fit.json" in m["files"]["solve.sh"] for m in tries)
    assert "printf '%s\\n' '{}' > /app/out/fit.json" in tries[1]["files"]["solve.sh"]
    assert trivial.submissions(task, "Explain your reasoning.") == []


async def test_tasks_a_model_already_solves_are_dropped_one_by_one(workspace, ident, session):
    calls = []

    async def reported(benchmark, repo, names):       # the benchmark's own results cover three of its tasks
        calls.append((benchmark, len(names)))
        return {"tasks": {"t0001": {"pass_rate": 0.9, "model": "some-model"}, "T0002": {"pass_rate": 0.5, "model": "some-model"},
                          "t0004": {"pass_rate": 0.67, "model": "other-model"}, "not-a-task": {"pass_rate": 1.0}},
                "source": "results/per_task.csv", "cost": 0.4}

    session.backend.reported = reported
    await session.run()
    bench = session.benches["demo-bench"]
    assert bench.triaged_out == 2 and calls == [("demo-bench", 24)]      # asked once, for the whole benchmark
    gates = {e["task_id"].split("/")[-1]: e for e in session.ledger.events if e["type"] == "decision" and e.get("gate") == "gates"}
    assert gates["t0001"]["reason"] == "reported:0.9" and gates["t0004"]["reason"] == "reported:0.67"
    assert gates["t0002"]["outcome"] == "pass" and gates["t0002"]["value"]["reported"]["value"] == 0.5      # at the limit, not over it
    assert "reported" not in gates["t0000"]["value"]                     # no reported result: it passes
    found = next(e for e in session.ledger.events if e.get("gate") == "reported_results")
    assert set(found["results"]) == {"t0001", "t0002", "t0004"} and found["source"] == "results/per_task.csv"

    from harbor_master import tasks
    from harbor_master.core import config as config_mod
    from harbor_master.engine import Session
    again = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=True,
                    speed=0, no_input=True, run_id="second")
    again.backend.reported = reported
    await again.run()
    assert len(calls) == 1                                               # the record is reused: no agent reads it twice


async def test_a_leaked_answer_fails_triage(workspace, session, tmp_path):
    task = tmp_path / "fake" / "demo-bench" / "t0003"
    (task / "environment").mkdir()
    (task / "solution" / "solve.sh").write_text("#!/bin/bash\n" + "echo 'a,b' > results.csv  # the whole answer\n" * 3)
    (task / "environment" / "setup.sh").write_bytes((task / "solution" / "solve.sh").read_bytes())
    await session.run()
    found = next(e for e in session.ledger.events if e["type"] == "decision" and e.get("gate") == "gates" and e["task_id"].endswith("t0003"))
    assert found["outcome"] == "drop" and found["reason"] == "leak:solution_in_image" and not found.get("cost")
    assert session.benches["demo-bench"].triaged_out == 1


async def test_the_difficulty_run_happens_once_per_task(workspace, session):
    await session.run()
    events = session.ledger.events
    suite = {(e.agent, e.model) for e in session.cfg.difficulty.suite}
    slots = Counter((e["task_id"], e["agent"], e["model"], e["trial_index"]) for e in events
                    if e["type"] == "trial" and e["purpose"] == "difficulty")
    assert slots and max(slots.values()) == 1                 # no frontier trial was run twice, whatever was repaired
    assert {(a, m) for _, a, m, _ in slots} <= suite

    measured = {}       # task -> the digest its frontier trials ran on
    for e in events:
        if e["type"] == "trial" and e["purpose"] == "difficulty":
            assert measured.setdefault(e["task_id"], e["digest"]) == e["digest"]
    carried = [e for e in events if e["type"] == "decision" and e.get("stage") == "difficulty" and e.get("carried_from")]
    assert carried, "the simulation repairs some task after its difficulty run"
    for e in carried:   # a task repaired afterwards keeps its result, on a record that names where it was measured
        assert e["outcome"] == "pass" and e["carried_from"] == measured[e["task_id"]] != e["digest"]
        assert any(r["type"] == "repair" and r["new_digest"] == e["digest"] for r in session.ledger.by_task[e["task_id"]])
    assert ledger.verify(workspace) == []


async def test_the_worker_model_tries_first_and_spares_the_frontier_trials(workspace, session):
    await session.run()
    events = session.ledger.events
    worker = session.cfg.tiers.worker.model
    cheap = [e for e in events if e["type"] == "trial" and e["purpose"] == "cheap_solve"]
    assert cheap and all(e["model"] == worker and e["agent"] == "claude-code" for e in cheap)
    tries = Counter(e["task_id"] for e in cheap)
    assert set(tries.values()) == {2}                          # two tries a task, run side by side
    passed = Counter(e["task_id"] for e in cheap if e["passed"])
    solved = {t for t, n in passed.items() if n == 2}
    once = {t for t, n in passed.items() if n == 1}
    assert solved, "the simulation lets the worker model solve some task twice"
    assert once and once <= {e["task_id"] for e in events if e["type"] == "trial" and e["purpose"] == "difficulty"}    # one pass of two is not enough
    frontier = {e["task_id"] for e in events if e["type"] == "trial" and e["purpose"] == "difficulty"}
    assert not solved & frontier                               # a task it solved never reached the suite
    for tid in solved:
        d = next(e for e in session.ledger.by_task[tid] if e["type"] == "decision" and e.get("stage") == "difficulty")
        assert d["outcome"] == "drop" and d["reason"] == "solved_by_worker" and d["cheap_solve"] == "passed" and d["cheap_trials"] == 2 and not d.get("trials")
    rows, _ = ledger.leaderboard(events, suite=[(e.agent, e.model) for e in session.cfg.difficulty.suite])
    assert all(r["model"] != worker for r in rows)             # its one try is not a leaderboard result


async def test_trivial_submissions_and_coverage_are_checked_before_any_frontier_trial(workspace, session):
    plain = session.backend.run_mutant

    async def run_mutant(task, mutant, stage="probe"):      # one task's verifier accepts an empty file, until repaired
        if task.name == "t0005" and task.digest == task.origin_digest and mutant["id"] == "trivial-empty":
            return True
        return await plain(task, mutant, stage)

    asked, covers = [], session.backend.covers

    async def unstated(task, first=None):                   # the first task asked about has an unstated check, until repaired
        d = await covers(task, first)
        asked.append(task.name)
        if task.name == asked[0] and task.digest == task.origin_digest:
            d.value = "unstated"
        return d

    session.backend.run_mutant, session.backend.covers = run_mutant, unstated
    await session.run()
    events = session.ledger.events
    order = {e["id"]: i for i, e in enumerate(events)}
    for gate, stage in (("trivial", "verify"), ("coverage", "probe")):
        found = [e for e in events if e["type"] == "decision" and e.get("gate") == gate and e.get("stage") == stage]
        assert found and {e["outcome"] for e in found} <= {"pass", "fix", "escalate"}
        assert any(e["outcome"] == "fix" for e in found), f"the simulation finds a {gate} defect in some task"
    for e in events:        # every frontier trial of a task comes after its last cheap check passed
        if e["type"] == "trial" and e["purpose"] == "difficulty":
            checks = [c for c in session.ledger.by_task[e["task_id"]] if c["type"] == "decision" and c["digest"] == e["digest"]
                      and c.get("gate") in ("trivial", "verifier", "coverage")]
            assert {c["gate"] for c in checks if c["outcome"] == "pass"} == {"trivial", "verifier", "coverage"}
            assert all(order[c["id"]] < order[e["id"]] for c in checks)


def test_the_owner_or_a_lead_makes_the_final_cut(workspace, ident, tmp_path, monkeypatch):
    """An accepted task is cut from the index, its list and its store, and can be put back. Only by the owner or a lead."""
    import tomllib

    import tomli_w
    from typer.testing import CliRunner

    from harbor_master.cli import app
    from harbor_master.core import config as config_mod
    from harbor_master.tasks import store
    from harbor_master.tasks.model import digest_dir

    people = tomllib.loads((workspace / ledger.CONTRIBUTORS).read_text())["contributor"]
    shared = tmp_path / "shared-store"
    (workspace / "about.toml").write_text(tomli_w.dumps({"name": "t", "tasks": str(shared)}))
    cfg = config_mod.load(workspace)
    assert cfg.about.tasks == str(shared)
    task_dir = tmp_path / "fake" / "demo-bench" / "t0001"
    digest = digest_dir(task_dir)
    shard = ledger.Shard(workspace, "demo-bench", ident, ledger.MANUAL, cfg.protocol)
    shard.append("report", "demo-bench/t0001", digest, recommendation="accept")
    shard.append("verdict", "demo-bench/t0001", digest, value="accept", reason="", reviewer=ident.github, tier="human")
    ledger.write_index(workspace)
    shared.mkdir()
    assert store.put(workspace, cfg, "demo-bench", "t0001", digest, task_dir)[0]
    key = store.key("demo-bench", "t0001", digest)
    assert (shared / key).is_file()
    run = lambda *a: CliRunner().invoke(app, [*a, "--workspace", str(workspace)])       # noqa: E731

    out = run("tasks", "cut", "t0001", "too", "close", "to", "t0002")
    assert out.exit_code != 0 and "neither" in out.output           # an ordinary contributor cannot

    people[0]["role"] = "lead"
    (workspace / ledger.CONTRIBUTORS).write_text(tomli_w.dumps({"contributor": people}))
    assert run("tasks", "cut", "t0001").exit_code != 0              # a reason is needed
    out = run("tasks", "cut", "t0001", "too", "close", "to", "t0002")
    assert out.exit_code == 0 and "Cut demo-bench/t0001" in out.output, out.output
    events = ledger.Ledger(workspace).events
    assert ledger.fold(events)["demo-bench/t0001"].status == "cut" and ledger.index_entries(events) == []
    assert "t0001" not in (workspace / ledger.INDEX_FILE).read_text()
    assert not (shared / key).exists() and store.local(workspace).has(key)      # out of the store, kept on this machine

    out = run("tasks", "restore", "t0001")
    assert out.exit_code == 0 and "Restored demo-bench/t0001" in out.output, out.output
    events = ledger.Ledger(workspace).events
    assert [e["task"] for e in ledger.index_entries(events)] == ["demo-bench/t0001"] and (shared / key).is_file()

    assert run("tasks", "cut", "t0001", "final", "cut").exit_code == 0
    people[0].pop("role")                                           # someone who is no lead: their cut does not verify
    (workspace / ledger.CONTRIBUTORS).write_text(tomli_w.dumps({"contributor": people}))
    assert any("neither the owner nor a lead" in p for p in ledger.verify(workspace))


def test_a_roster_entry_takes_a_preset_by_its_short_name(tmp_path, monkeypatch):
    import pytest

    from harbor_master.core import config as config_mod
    from harbor_master.core import keys

    assert len(config_mod.presets()) == 10
    (tmp_path / "roster.toml").write_text(
        '[[config]]\nuse = "gpt-6-astra"\n\n'
        '[[config]]\nuse = "claude-opus-5.5"\nname = "opus-pinned"\nversion = "2.1.292"\n\n'
        '[[config]]\nname = "mine"\nagent = "mini-swe-agent"\nmodel = "openrouter/qwen/qwen3.8-27b"\n')
    astra, opus, mine = config_mod.roster(tmp_path)
    assert (astra.name, astra.agent, astra.model, astra.version, astra.reasoning_effort) == ("gpt-6-astra", "codex", "gpt-6-astra", "0.162.1", "max")
    assert (opus.name, opus.version, opus.model) == ("opus-pinned", "2.1.292", "claude-opus-5-5") and opus.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "128000"
    assert mine.key == "" and keys.needs(mine.agent, mine.model)[-1] == "OPENROUTER_API_KEY"    # read off the model's provider
    with pytest.raises(ValueError, match="no preset is named"):
        config_mod.SuiteEntry.model_validate({"use": "nothing"})
    suite = config_mod.Difficulty.model_validate({"suite": [{"use": "gpt-6-astra"}]}).suite      # the difficulty suite takes one too
    assert suite[0].agent == "codex" and suite[0].version == "0.162.1"

    listed = keys.harbor_agents()                   # Harbor is a dependency: its agents are the ones a roster may name
    assert {"claude-code", "codex", "muse-code", "mini-swe-agent"} <= set(listed)
    assert all(keys.known_agent(p["agent"]) for p in config_mod.presets().values()) and not keys.known_agent("no-such-agent")
    assert "ANTHROPIC_API_KEY" in listed["claude-code"] and listed["muse-code"] == ["META_API_KEY"]

    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "CODEX_AUTH_JSON_PATH", "OPENROUTER_API_KEY", "HF_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("META_API_KEY", "meta-value")
    monkeypatch.setenv("ZAI_API_KEY", "zai-value")
    monkeypatch.setenv("HF_TOKEN", "hf-value")
    env = keys.agent_env("muse-code", "muse-wonder-1.0", "META_API_KEY")
    assert env == {"META_API_KEY": "meta-value"}                                    # its own key, and no other
    env = keys.agent_env("claude-code", "glm-5.3", "ZAI_API_KEY", {"ANTHROPIC_API_KEY": "${ZAI_API_KEY}"})
    assert env == {"ZAI_API_KEY": "zai-value"}                                      # a key its settings name is passed on
