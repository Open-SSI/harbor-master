"""A trial whose logs stop moving is read by the worker agent; a hung one is stopped, and run again or its task dropped."""
import asyncio
import stat

import pytest

from harbor_master import tasks
from harbor_master.backends import harbor
from harbor_master.backends.base import TaskDropped
from harbor_master.core import config as config_mod
from harbor_master.engine import Session


async def test_a_trial_that_writes_nothing_is_judged_and_stopped(tmp_path, monkeypatch):
    fake = tmp_path / "harbor"                  # a harbor that starts and then never moves again
    fake.write_text("#!/bin/sh\necho starting\nexec sleep 600\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(harbor, "binary", lambda: str(fake))
    asked = []

    async def judge(log, quiet):
        asked.append(log)
        return "wait" if len(asked) == 1 else "drop"         # still working, once; then hung, and the task is dropped
    watch = harbor.Watch(every=0.1, quiet=0.3, judge=judge)
    with pytest.raises(TaskDropped):
        await asyncio.wait_for(harbor.run(tmp_path / "task", tmp_path / "jobs", "oracle", watch=watch), 20)
    assert len(asked) == 2 and "starting" in asked[0]     # it read what the trial printed


async def test_a_hung_trial_is_run_again_or_its_task_dropped_as_the_person_says(workspace, ident, monkeypatch):
    dropped, retried = [t.name for t in sorted((workspace.parent / "fake" / "demo-bench").iterdir())[:2]]
    monkeypatch.setenv("HM_SIMULATE_HANG", f"{dropped},{retried}")
    s = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, speed=0,
                no_input=True, run_id="r-hung", only=[dropped, retried])
    boxes = []

    async def answer(kind, text, target="", choices=None):     # the person, at the box
        if kind == "drop":
            return "continue"           # a check's rejection is let go: these tasks are here for their trials
        if kind != "hung":
            return None
        boxes.append(text)
        return "drop" if target.endswith("/" + dropped) else "retry"
    monkeypatch.setattr(s, "ask", answer)
    await s.run()
    of = lambda name: [e for e in s.ledger.events if e.get("task_id", "").endswith("/" + name)]  # noqa: E731
    assert len(boxes) == 2 and all("The worker agent read its log" in b for b in boxes)
    assert any(e.get("gate") == "hung" and e.get("tier") == "worker" for e in of(dropped) + of(retried))
    assert any(e.get("gate") == "hung" and e.get("outcome") == "drop" for e in of(dropped))
    assert not any(e["type"] == "report" for e in of(dropped))                     # dropped: no report
    assert not any(e.get("gate") == "hung" and e.get("outcome") == "drop" for e in of(retried))
    assert any(e["type"] == "trial" for e in of(retried))                         # run again, and recorded


def test_an_agents_program_is_downloaded_once_and_mounted_into_each_container(tmp_path, monkeypatch):
    from harbor_master.backends import agents
    monkeypatch.setenv("HM_AGENT_CACHE", str(tmp_path / "cache"))
    calls = []
    monkeypatch.setattr(agents, "_fetch", lambda agent, version, name="": calls.append(agent) or b"#!/bin/sh\necho 1.2.3\n")
    first, again = agents.mounts("claude-code", "1.2.3"), agents.mounts("claude-code", "1.2.3")
    assert first == again == [{"type": "bind", "source": str(tmp_path / "cache/claude-code/1.2.3/claude"),
                               "target": "/usr/local/bin/claude", "read_only": True}] and calls == ["claude-code"]
    assert (tmp_path / "cache/claude-code/1.2.3/claude").stat().st_mode & 0o111        # it can be run
    monkeypatch.setattr(agents, "_fetch", lambda agent, version, name="": 1 / 0)                 # no network: Harbor installs it
    assert agents.mounts("codex", "9.9.9") == [] and agents.mounts("oracle", "") == []


async def test_a_long_trial_says_what_it_is_doing_every_few_minutes(tmp_path, monkeypatch):
    fake = tmp_path / "harbor"                  # a harbor that runs for a while, and finishes with no trial
    fake.write_text("#!/bin/sh\nsleep 2.5\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(harbor, "binary", lambda: str(fake))
    monkeypatch.setattr(harbor, "inside", lambda job, lines=60: (0, "=== /logs/agent/oracle.txt 9\nTriangulating: 40%\rTriangulating: 92%"))
    said = []

    async def judge(log, quiet):
        return "wait"
    watch = harbor.Watch(every=0.2, quiet=60, judge=judge, say=lambda doing, ran: said.append(doing), say_every=0.5)
    await harbor.run(tmp_path / "task", tmp_path / "jobs", "oracle", watch=watch)
    assert len(said) >= 2 and said[0] == "Triangulating: 92%"          # a progress bar as it stands now


def test_an_agents_latest_step_is_read_from_its_event_stream():
    stream = "\n".join(['{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"python fit.py --all"}}]}}',
                        '{"type":"system","subtype":"thinking_tokens","estimated_tokens":1400}'])
    assert harbor.now_doing(stream) == "running fit.py"
    heredoc = "cat > /root/work/overdisp.py << 'EOF'\nimport sys, numpy as np\nEOF"
    assert harbor.plain_command(heredoc) == "writing overdisp.py"           # what it does, not its text
    assert harbor.plain_command("python3 - <<'EOF'\nimport numpy\nEOF") == "running a Python snippet"
    assert harbor.plain_command("pip install scanpy") == "installing packages"
    assert harbor.plain_command("ls -la /root/data") == "looking at files"
    codex = '{"type":"item.started","item":{"id":"i3","type":"command_execution","command":"bash -lc \'python run_fit.py\'"}}'
    assert harbor.now_doing(codex) == "running run_fit.py"
    said = '{"type":"assistant","message":{"content":[{"type":"text","text":"The fit converged. Next I check residuals."}]}}'
    assert harbor.now_doing(said) == "says: The fit converged."
    assert harbor.now_doing('{"type":"system","subtype":"init"}') == "thinking"


def test_a_reference_solution_over_the_verify_limit_drops_its_task():
    from harbor_master.engine.prepare import SLOW, slow_build
    error = "AgentTimeoutError: Agent execution timed out after 1800.0 seconds"
    assert SLOW.search(error)
    assert slow_build(error, {"max_verify_minutes": 30}) == \
        "its reference solution did not finish within 30 minutes, the protocol's limit"


def test_codex_is_mounted_with_the_host_it_runs_commands_in(tmp_path, monkeypatch):
    from harbor_master.backends import agents
    monkeypatch.setenv("HM_AGENT_CACHE", str(tmp_path))
    monkeypatch.setattr(agents, "_fetch", lambda agent, version, name="": b"#!/bin/sh\n")
    targets = [m["target"] for m in agents.mounts("codex", "9.9.9")]
    assert targets == ["/usr/local/bin/codex", "/usr/local/bin/codex-code-mode-host"]

    def fails(agent, version, name=""):
        raise OSError("no network")
    monkeypatch.setattr(agents, "_fetch", fails)
    assert agents.mounts("codex", "9.9.8") == []        # never codex without its host: Harbor installs it instead


def test_the_checks_before_a_run_report_each_one_as_it_ends(monkeypatch, tmp_path):
    from harbor_master.core import access
    monkeypatch.setattr(access, "jobs_of", lambda cfg, ws: {"Docker": lambda: "", "codex with m, in a container": lambda: "it could not run"})
    seen = []
    missing = access.check(None, tmp_path, lambda what, why, seconds: seen.append((what, why)))
    assert sorted(seen) == [("Docker", ""), ("codex with m, in a container", "it could not run")]
    assert missing == ["it could not run"]


def test_a_command_is_said_by_what_it_does():
    assert harbor.plain_command("cd /root/work && python em.py --iters 50") == "running em.py"
    assert harbor.plain_command("sleep 60") == "waiting"
    assert harbor.plain_command("cd /root/work; python3 - <<'E'\nimport re\nE") == "running a Python snippet"


def test_a_running_agents_tokens_are_counted_once_per_message():
    out = "\n".join(['"id":"msg_a"', '"input_tokens":2', '"cache_creation_input_tokens":100', '"cache_read_input_tokens":1000',
                     '"output_tokens":5', '"id":"msg_a"', '"input_tokens":2', '"cache_creation_input_tokens":100',
                     '"cache_read_input_tokens":1000', '"output_tokens":40',          # the same message's next part
                     '"id":"msg_b"', '"input_tokens":3', '"cache_read_input_tokens":1100', '"output_tokens":10',
                     '"type":"result"', '"input_tokens":999', '"output_tokens":999'])  # the summary: already counted
    assert harbor.tokens_so_far("claude-code", out) == (5, 2100, 100, 50)
    codex = '"total_token_usage":{"input_tokens":46011,"cached_input_tokens":39424,"cache_write_input_tokens":0,"output_tokens":846}'
    assert harbor.tokens_so_far("codex", codex) == (6587, 39424, 0, 846)
