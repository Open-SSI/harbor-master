"""The cheap filters, the once-only difficulty run, the final cut, presets, providers and costs (DESIGN.md Sections 2, 4)."""
import tomllib
from collections import Counter

import pytest
import tomli_w
from typer.testing import CliRunner

from harbor_master import ledger, tasks
from harbor_master.backends import NotImplementedBackend, claude, codex, sessions
from harbor_master.core import config as config_mod
from harbor_master.core import keys, prices
from harbor_master.core.config import Tier
from harbor_master.curation import leak, trivial
from harbor_master.engine import Session
from harbor_master.tasks import store
from harbor_master.tasks.model import digest_dir

SOLVE = ("#!/bin/bash\npython - <<'PY'\nimport json, statistics\n"
         "json.dump({'k': statistics.mean([1.0, 2.0, 4.5])}, open('/app/out/fit.json', 'w'))\nPY\n")


def _task(root, instruction="Read /app/data/in.json and write the fit to /app/out/fit.json. See notes.txt.\n"):
    for sub in ("environment", "solution", "tests"):
        (root / sub).mkdir(parents=True)
    (root / "instruction.md").write_text(instruction)
    (root / "environment" / "in.json").write_text("{}")
    (root / "solution" / "solve.sh").write_text(SOLVE)
    (root / "tests" / "test_fit.py").write_text("import json\nassert abs(json.load(open('/app/out/fit.json'))['k'] - 2.5) < 1e-6\n")
    return root


def test_the_leak_scan(tmp_path):
    def found(name, plant=lambda t: None, instruction=""):
        task = _task(tmp_path / name)
        plant(task)
        return leak.find(task, instruction)[0]

    copy = lambda src, dst: lambda t: (t / dst).write_bytes((t / src).read_bytes())      # noqa: E731
    assert found("clean") == leak.NONE
    assert found("solution", copy("solution/solve.sh", "environment/helper.sh")) == "solution_in_image"
    assert found("tests", copy("tests/test_fit.py", "environment/check.py")) == "tests_in_image"
    assert found("reach", lambda t: (t / "environment" / "Dockerfile").write_text("COPY ../solution /opt/ref\n")) == "solution_in_image"
    assert found("quoted", instruction="Do this:\n" + SOLVE) == leak.NONE          # two lines of a solution are too few to call
    lines = "".join(f"result_{i} = compute_the_value(measurements, step={i})\n" for i in range(4))
    assert found("quoted2", lambda t: (t / "solution" / "more.py").write_text(lines), "Do this:\n" + lines) == "solution_in_instruction"


def test_trivial_submissions_are_written_for_the_files_a_task_names(tmp_path):
    task = _task(tmp_path / "t")
    text = (task / "instruction.md").read_text()
    assert trivial.outputs(task, text) == ["/app/out/fit.json"]       # not the input, and not a file the tests never name
    tries = trivial.submissions(task, text)
    assert [m["id"] for m in tries] == ["trivial-empty", "trivial-placeholder", "trivial-copy"]
    assert "printf '%s\\n' '{}' > /app/out/fit.json" in tries[1]["files"]["solve.sh"]
    assert trivial.submissions(task, "Explain your reasoning.") == []


async def test_triage_drops_a_leaked_task_and_tasks_a_model_already_solves(workspace, ident, session, tmp_path):
    calls = []

    async def reported(benchmark, repo, names):       # the benchmark's own results cover three of its tasks
        calls.append(len(names))
        return {"tasks": {"t0001": {"pass_rate": 0.9, "model": "m"}, "T0002": {"pass_rate": 0.5, "model": "m"},
                          "not-a-task": {"pass_rate": 1.0}}, "source": "results.csv", "cost": 0.4}

    task = tmp_path / "fake" / "demo-bench" / "t0003"
    (task / "environment").mkdir()
    (task / "solution" / "solve.sh").write_text(SOLVE)
    (task / "environment" / "setup.sh").write_text(SOLVE)
    session.backend.reported = reported
    await session.run()
    gates = {e["task_id"].split("/")[-1]: e for e in session.ledger.events if e["type"] == "decision" and e.get("gate") == "gates"}
    assert gates["t0001"]["reason"] == "reported:0.9" and gates["t0003"]["reason"] == "leak:solution_in_image"
    assert gates["t0002"]["outcome"] == "pass" and "reported" not in gates["t0000"]["value"]     # at the limit, or no result: it passes
    assert session.benches["demo-bench"].triaged_out == 2 and calls == [24]

    again = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=True,
                    speed=0, no_input=True, run_id="second")
    again.backend.reported = reported
    await again.run()
    assert calls == [24]                                # the record is reused: no agent reads the results twice


async def test_cheap_checks_come_first_and_the_difficulty_run_happens_once(workspace, session):
    mutant, covers, asked = session.backend.run_mutant, session.backend.covers, []

    async def run_mutant(task, m, stage="probe"):       # one task's verifier accepts an empty file, until repaired
        return (task.name == "t0005" and task.digest == task.origin_digest and m["id"] == "trivial-empty") or await mutant(task, m, stage)

    async def unstated(task, first=None):               # the first task asked about has an unstated check, until repaired
        d = await covers(task, first)
        asked.append(task.name)
        if task.name == asked[0] and task.digest == task.origin_digest:
            d.value = "unstated"
        return d

    session.backend.run_mutant, session.backend.covers = run_mutant, unstated
    await session.run()
    events, by = session.ledger.events, session.ledger.by_task
    order = {e["id"]: i for i, e in enumerate(events)}
    hard = [e for e in events if e["type"] == "trial" and e["purpose"] == "difficulty"]
    for gate in ("trivial", "coverage"):
        assert any(e.get("gate") == gate and e["outcome"] == "fix" for e in events)
    for e in hard:      # every frontier trial of a task comes after its cheap checks passed
        checks = [c for c in by[e["task_id"]] if c["type"] == "decision" and c["digest"] == e["digest"]
                  and c.get("gate") in ("trivial", "verifier", "coverage")]
        assert {c["gate"] for c in checks if c["outcome"] == "pass"} == {"trivial", "verifier", "coverage"}
        assert all(order[c["id"]] < order[e["id"]] for c in checks)

    # Once per task: no frontier trial is run twice, and a task repaired afterwards carries its result forward.
    assert max(Counter((e["task_id"], e["agent"], e["model"], e["trial_index"]) for e in hard).values()) == 1
    measured = {e["task_id"]: e["digest"] for e in hard}
    carried = [e for e in events if e.get("stage") == "difficulty" and e.get("carried_from")]
    assert carried and all(e["outcome"] == "pass" and e["carried_from"] == measured[e["task_id"]] != e["digest"] for e in carried)

    # The worker model tries twice first. A task it solves both times never reaches the suite.
    worker = session.cfg.tiers.worker.model
    cheap = [e for e in events if e["type"] == "trial" and e["purpose"] == "cheap_solve"]
    assert set(Counter(e["task_id"] for e in cheap).values()) == {2} and all(e["model"] == worker for e in cheap)
    passed = Counter(e["task_id"] for e in cheap if e["passed"])
    solved, once = {t for t, n in passed.items() if n == 2}, {t for t, n in passed.items() if n == 1}
    assert solved and once and not solved & set(measured) and once <= set(measured)
    drops = [next(e for e in by[t] if e.get("stage") == "difficulty") for t in solved]
    assert all(d["outcome"] == "drop" and d["reason"] == "solved_by_worker" and d["cheap_trials"] == 2 for d in drops)
    rows, _ = ledger.leaderboard(events, suite=[(e.agent, e.model) for e in session.cfg.difficulty.suite])
    assert all(r["model"] != worker for r in rows) and ledger.verify(workspace) == []


def test_the_owner_or_a_lead_makes_the_final_cut(workspace, ident, tmp_path):
    people = tomllib.loads((workspace / ledger.CONTRIBUTORS).read_text())["contributor"]
    shared, task_dir = tmp_path / "shared-store", tmp_path / "fake" / "demo-bench" / "t0001"
    shared.mkdir()
    (workspace / "about.toml").write_text(tomli_w.dumps({"name": "t", "tasks": str(shared)}))
    cfg, digest = config_mod.load(workspace), digest_dir(task_dir)
    shard = ledger.Shard(workspace, "demo-bench", ident, ledger.MANUAL, cfg.protocol)
    shard.append("report", "demo-bench/t0001", digest, recommendation="accept")
    shard.append("verdict", "demo-bench/t0001", digest, value="accept", reason="", reviewer=ident.github, tier="human")
    ledger.write_index(workspace)
    key = store.key("demo-bench", "t0001", digest)
    assert store.put(workspace, cfg, "demo-bench", "t0001", digest, task_dir)[0] and (shared / key).is_file()

    def role(value):
        people[0].pop("role", None)
        (workspace / ledger.CONTRIBUTORS).write_text(tomli_w.dumps({"contributor": [{**people[0], **({"role": value} if value else {})}]}))

    run = lambda *a: CliRunner().invoke(app, [*a, "--workspace", str(workspace)])       # noqa: E731
    index = lambda: [e["task"] for e in ledger.index_entries(ledger.Ledger(workspace).events)]   # noqa: E731
    from harbor_master.cli import app
    assert "neither" in run("cut", "t0001", "too", "close").output             # an ordinary contributor cannot
    role("lead")
    assert run("cut", "t0001").exit_code != 0                                  # a reason is needed
    assert "Cut demo-bench/t0001" in run("cut", "t0001", "too", "close").output
    assert index() == [] and "t0001" not in (workspace / ledger.INDEX_FILE).read_text()
    assert not (shared / key).exists() and store.local(workspace).has(key)              # out of the store, kept on this machine
    assert "Restored" in run("restore", "t0001").output and index() == ["demo-bench/t0001"] and (shared / key).is_file()
    assert run("cut", "t0001", "final").exit_code == 0
    role("")                                                                            # no lead any more: their cut does not verify
    assert any("neither the owner nor a lead" in p for p in ledger.verify(workspace))


def test_presets_agents_and_the_keys_a_trial_gets(tmp_path, monkeypatch):
    assert len(config_mod.presets()) == 10
    (tmp_path / "roster.toml").write_text(
        '[[config]]\nuse = "gpt-6-astra"\n\n[[config]]\nuse = "claude-opus-5.5"\nname = "pinned"\nversion = "2.1.292"\n\n'
        '[[config]]\nname = "mine"\nagent = "mini-swe-agent"\nmodel = "openrouter/qwen/qwen3.8-27b"\n')
    astra, opus, mine = config_mod.roster(tmp_path)
    assert (astra.name, astra.agent, astra.version, astra.reasoning_effort) == ("gpt-6-astra", "codex", "0.162.1", "max")
    assert (opus.name, opus.version, opus.model) == ("pinned", "2.1.292", "claude-opus-5-5") and opus.env
    assert keys.needs(mine.agent, mine.model)[-1] == "OPENROUTER_API_KEY"               # read off the model's provider
    assert config_mod.Difficulty.model_validate({"suite": [{"use": "gpt-6-astra"}]}).suite[0].agent == "codex"
    with pytest.raises(ValueError, match="no preset is named"):
        config_mod.SuiteEntry.model_validate({"use": "nothing"})

    listed = keys.harbor_agents()                       # Harbor is a dependency: its agents are the ones a roster may name
    assert all(keys.known_agent(p["agent"]) for p in config_mod.presets().values()) and not keys.known_agent("no-such-agent")
    assert "ANTHROPIC_API_KEY" in listed["claude-code"] and listed["muse-code"] == ["META_API_KEY"]

    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "CODEX_AUTH_JSON_PATH", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("META_API_KEY", "ZAI_API_KEY", "HF_TOKEN"):
        monkeypatch.setenv(name, name.lower())
    assert keys.agent_env("muse-code", "muse-wonder-1.0", "META_API_KEY") == {"META_API_KEY": "meta_api_key"}     # its own key, no other
    assert keys.agent_env("claude-code", "glm-5.3", "ZAI_API_KEY", {"ANTHROPIC_API_KEY": "${ZAI_API_KEY}"}) == {"ZAI_API_KEY": "zai_api_key"}


async def test_tiers_run_on_claude_or_codex_and_a_session_with_no_cost_is_estimated(workspace, ident, tmp_path, monkeypatch):
    def answers(cost):
        async def ask(model, *a, **k):
            return claude.Reply({"ok": model}, cost, 130_000, fresh_in=100_000, cached=20_000, out=10_000)
        return ask

    monkeypatch.setattr(claude, "ask", answers(9.99))   # Claude reports a cost: it is recorded as it is
    monkeypatch.setattr(codex, "ask", answers(0.0))     # the Codex SDK reports tokens and no price
    ask = lambda provider, model: sessions.ask(Tier(provider=provider, model=model), "s", "p", {})    # noqa: E731
    assert (await ask("claude-agent-sdk", "claude-opus-5-5")).cost == 9.99
    assert (await ask("codex", "gpt-5.6-terra")).cost == round((100_000 * 2.00 + 20_000 * 0.20 + 10_000 * 12.00) / 1e6, 6) == 0.324
    assert (await ask("codex", "an-unpriced-model")).cost == 0.0 and prices.estimate("an-unpriced-model", 10) is None
    assert prices.estimate("claude-sonnet-5-5", 100_000, 400_000, 50_000, 20_000) == 0.565
    with pytest.raises(NotImplementedBackend, match="not built"):
        await ask("other", "m")

    # Codex uses the sign-in file, never a key: the file CODEX_AUTH_JSON_PATH names, else ~/.codex/auth.json.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CODEX_AUTH_JSON_PATH", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "a-key")
    with pytest.raises(NotImplementedBackend, match="codex login"):
        codex._env()
    (tmp_path / "home" / ".codex").mkdir(parents=True)
    (tmp_path / "home" / ".codex" / "auth.json").write_text("{}")
    assert codex._env() == {"CODEX_HOME": str(tmp_path / "home" / ".codex")}

    # The worker tier may be on Codex. The decision tier runs on Jev only.
    real = lambda cfg: Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=False)    # noqa: E731
    cfg = config_mod.load(workspace)
    cfg.tiers.worker.provider, cfg.tiers.worker.model = "codex", "gpt-5.6-terra"
    real(cfg)
    cfg.tiers.decision.provider = "claude-agent-sdk"
    with pytest.raises(NotImplementedBackend, match="Jev only"):
        real(cfg)


async def test_a_simulated_run_is_a_walk_through_of_the_stages(session):
    from harbor_master.interact import guide, walk
    await session.run()
    names = [name for name, _ in session.walk]
    assert names[:3] == ["triage", "rank", "verify"] and set(names) == set(walk.STAGE) - {"select"}     # each once. No sign-off was asked here
    told = [e for e in session.recent if e[1] == "stage"]
    assert len(told) == len(names) and all(text == walk.STAGE[stage] for _, _, stage, text in told)
    session.finished, session.cells[0].task = False, session.benches["demo-bench"].tasks[0]            # while a task is in a cell
    assert guide.session_tip(session).startswith(names[-1].capitalize() + ". ")                         # the tip says what the stage does
    session.simulate, session.walk = False, []
    session.explain("verify")
    assert session.walk == []                                                                          # a real run is not narrated


async def test_a_simulation_takes_every_task_through_the_stages_and_leaves_the_protocol_alone(workspace, ident):
    cfg = config_mod.load(workspace)
    cfg.select.top_k, cfg.select.max_fraction = 24, 0.25                # a real run would take a quarter: 6 of 24
    (workspace / config_mod.PROTOCOL_NAME).write_text(tomli_w.dumps({k: getattr(cfg, k).model_dump() for k in config_mod.PROTOCOL_SECTIONS}))
    cfg = config_mod.load(workspace)
    s = Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=True, speed=0, no_input=True)
    await s.run()
    bench = s.benches["demo-bench"]
    assert bench.selected == 24 - bench.triaged_out and bench.not_selected == 0
    assert s.cfg.select.max_fraction == 0.25 and s.protocol == cfg.protocol       # the setting was not touched to do it
    s._check_config()                                                   # so the file is not found to differ from the run
    assert not s._config_changed and not any(kind == "config" for _, kind, _, _ in s.recent)


async def test_one_person_on_two_machines_cannot_run_the_same_tasks(workspace, ident):
    from harbor_master.engine import claims

    def on(machine, run):
        s = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=True,
                    speed=0, no_input=True, run_id=run)
        s.machine = machine
        s.benches["demo-bench"].tasks = tasks.load_benchmark(s.benches["demo-bench"].path, "demo-bench")
        return s, s.benches["demo-bench"]

    (a, bench_a), (b, bench_b) = on("aaaa1111", "a1"), on("bbbb2222", "b1")       # the same person, signed in on two machines
    got_a = await claims.take(a, bench_a, bench_a.tasks[:6])
    b.ledger.reload()
    got_b = await claims.take(b, bench_b, bench_b.tasks[3:9])
    assert len(got_a) == 6 and [t.name for t in got_b] == [t.name for t in bench_b.tasks[6:9]]     # only what the first does not hold
    assert any("you, on another machine" in text for _, _, _, text in b.recent)
    assert all(e["machine"] in ("aaaa1111", "bbbb2222") for e in a.ledger.events + b.ledger.events if e["type"] == "claim")

    again, bench = on("aaaa1111", "a2")             # the first machine again, after its run died: its own claims do not shut it out
    assert len(await claims.take(again, bench, bench.tasks[:6])) == 6
    named = Session(workspace, config_mod.load(workspace), ident, {}, simulate=True)       # a run's files carry its machine's name,
    assert named.run_id.endswith("-" + named.machine[:4]) and len(named.machine) == 8      # so two machines never write the same file
