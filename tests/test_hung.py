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
    assert any(e["type"] == "trial" and e.get("purpose") == "oracle" for e in of(retried))   # run again, and recorded


def test_an_agents_program_is_downloaded_once_and_mounted_into_each_container(tmp_path, monkeypatch):
    from harbor_master.backends import agents
    monkeypatch.setenv("HM_AGENT_CACHE", str(tmp_path / "cache"))
    calls = []
    monkeypatch.setattr(agents, "_fetch", lambda agent, version: calls.append(agent) or b"#!/bin/sh\necho 1.2.3\n")
    first, again = agents.mounts("claude-code", "1.2.3"), agents.mounts("claude-code", "1.2.3")
    assert first == again == [{"type": "bind", "source": str(tmp_path / "cache/claude-code/1.2.3/claude"),
                               "target": "/usr/local/bin/claude", "read_only": True}] and calls == ["claude-code"]
    assert (tmp_path / "cache/claude-code/1.2.3/claude").stat().st_mode & 0o111        # it can be run
    monkeypatch.setattr(agents, "_fetch", lambda agent, version: 1 / 0)                 # no network: Harbor installs it
    assert agents.mounts("codex", "9.9.9") == [] and agents.mounts("oracle", "") == []
