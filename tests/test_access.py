"""Before a real curation: every key, the task store, the index's repository and Docker are tried once, and every problem
is listed together."""


from harbor_master.core import access, keys
from harbor_master.core import config as config_mod


def test_every_problem_is_listed_together_and_no_key_is_shown(workspace, monkeypatch):
    cfg = config_mod.load(workspace)
    secret = "or-abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setattr(keys, "get", lambda name: secret if name == "OPENROUTER_API_KEY" else None)
    monkeypatch.setattr(access, "openrouter", lambda key: "it was refused (HTTP 401: wrong or expired)")
    monkeypatch.setitem(access.KEY_CHECKS, "OPENROUTER_API_KEY", access.openrouter)
    monkeypatch.setattr(access, "store", lambda cfg: "The task store: Your token cannot write to it.")
    monkeypatch.setattr(access, "docker", lambda: "Docker is not running, or this user may not use it. Start it, then try again.")
    monkeypatch.setattr(access, "claude_signin", lambda model: "")         # no model is called, and no container started
    monkeypatch.setattr(access, "agent_runs", lambda agent, model, version: "")
    found = access.problems(cfg, workspace)
    told = "\n".join(found)
    assert "OPENROUTER_API_KEY" in told and "wrong or expired" in told and secret not in told
    assert "cannot write" in told and "Docker is not running" in told and len(found) == 3     # the clone has no remote: no push to try

    monkeypatch.setattr(access, "openrouter", lambda key: "")
    monkeypatch.setitem(access.KEY_CHECKS, "OPENROUTER_API_KEY", access.openrouter)
    monkeypatch.setattr(access, "store", lambda cfg: "")
    monkeypatch.setattr(access, "docker", lambda: "")
    assert access.problems(cfg, workspace) == []                    # everything answers: the run may start
