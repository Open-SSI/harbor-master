from harbor_master.core import config as config_mod
from harbor_master.core import keys


def test_protocol_changes_with_the_filter_but_not_with_parallelism():
    a, b, c = config_mod.Config(), config_mod.Config(), config_mod.Config()
    b.run.cells = 16
    c.difficulty.max_pass_rate = 0.5
    assert a.protocol == b.protocol != c.protocol


def test_keys_are_scrubbed(ident, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "abcd1234efgh5678")
    out = keys.scrub("token abcd1234efgh5678 and sk-ant-REDACTEDREDACTEDREDACTED00")
    assert "abcd1234efgh5678" not in out and "sk-ant" not in out


def test_the_config_template_matches_the_defaults_in_code():
    import tomllib
    from pathlib import Path
    from harbor_master import templates
    protocol, mine = tomllib.loads(templates.read("protocol.toml")), tomllib.loads(templates.read("hm.toml"))
    assert set(protocol) == {*config_mod.PROTOCOL_SECTIONS, "requires"} and set(mine) == {"run"}     # a clean split
    assert config_mod.Config.model_validate({**protocol, **mine}) == config_mod.Config()
    root = Path(__file__).parent.parent
    assert (root / "hm.example.toml").read_text() == templates.read("hm.toml")
    assert (root / "secrets.env.example").read_text() == templates.read("secrets.env.example")
    assert not (root / "registry").exists()            # no index lives in the tool's own repository


def test_an_unfilled_keys_file_counts_as_no_keys(ident, monkeypatch):
    from harbor_master import templates
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY", "HF_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    keys.secrets_path().write_text(templates.read("secrets.env.example"))
    assert keys.get("ANTHROPIC_API_KEY") is None
    assert not any(present for what, var, present, uses in keys.doctor(config_mod.Config()) if var != "none needed")
    keys.set_key("OPENROUTER_API_KEY", "or-abc123")
    assert keys.get("OPENROUTER_API_KEY") == "or-abc123"


def test_codex_takes_a_key_or_a_sign_in_file_and_the_file_must_exist(ident, monkeypatch, tmp_path):
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "CODEX_AUTH_JSON_PATH", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    codex = lambda: next(present for what, var, present, uses in keys.doctor(config_mod.Config()) if "CODEX" in var)  # noqa: E731
    auth = tmp_path / "auth.json"
    keys.set_key("CODEX_AUTH_JSON_PATH", str(auth))
    assert not codex() and not keys.found("OPENAI_API_KEY", "codex")       # the path is set, the file is not there
    auth.write_text('{"tokens": {"access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6.eyJpc3MiOiJodHRwczovL2F1dGg.c2lnbmF0dXJlLXNpZ25hdHVyZQ"}}')
    assert codex() and keys.found("OPENAI_API_KEY", "codex")
    assert not keys.found("OPENAI_API_KEY", "terminus-2")                  # the sign-in is Codex's own
    assert keys.agent_env() == {"CODEX_AUTH_JSON_PATH": str(auth.resolve())}
    assert str(auth) in keys.scrub(f"reading {auth}") and "eyJ" not in keys.scrub(auth.read_text())
    keys.set_key("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-abcdefghijklmnopqrstuvwx")
    assert keys.agent_env()["CLAUDE_FORCE_OAUTH"] == "1"                   # the sign-in is used when both could be


BENCHMARKS = '''
[[benchmark]]
id = "tb-science"
name = "Terminal-Bench-Science"
version = "0.1.0"
released = "2026-08-26"
github = "https://github.com/harbor-framework/terminal-bench-science"
ref = "v0.1.0"
commit = "f81afac4f11048e77a15dfc8fb1dbfb897fea0ce"
tasks = "tasks"
'''


def test_the_benchmark_list_comes_from_signed_records_and_the_roster_from_the_index(tmp_path, ident):
    import tomllib
    from harbor_master import ledger, tasks
    from harbor_master.core.config import roster
    assert tasks.supported.supported(tmp_path) == [] and roster(tmp_path) == []       # a new index lists nothing
    entry = {**tomllib.loads(BENCHMARKS)["benchmark"][0], "description": "Science tasks in a terminal.", "paper": "none"}
    ledger.benchmarks.add(tmp_path, ident, "p", {k: v for k, v in entry.items() if k != "ref"})       # not pinned: it does not count
    assert tasks.supported.supported(tmp_path) == []
    ledger.benchmarks.add(tmp_path, ident, "p", entry)
    (tmp_path / "roster.toml").write_text('[[config]]\nname = "astra"\nagent = "codex"\nmodel = "gpt-6-astra"\n'
                                          'reasoning_effort = "max"\nversion = "0.162.1"\nkey = "OPENAI_API_KEY"\n')
    first = tasks.supported.supported(tmp_path)[0]
    assert (first["id"], first["released"], first["ref"], first["added_by"]) == ("tb-science", "2026-08-26", "v0.1.0", ident.github)
    assert tasks.supported.find(tmp_path, "TB-Science")["name"] == "Terminal-Bench-Science"
    assert tasks.supported.find(tmp_path, "smdd-bench") is None
    astra = roster(tmp_path)[0]
    assert (astra.name, astra.agent, astra.model, astra.reasoning_effort, astra.version) == ("astra", "codex", "gpt-6-astra", "max", "0.162.1")

    ledger.benchmarks.write_list(tmp_path)                                      # one readable list, which changes nothing when edited
    view = tmp_path / "benchmarks.toml"
    assert tomllib.loads(view.read_text())["benchmark"][0]["commit"] == entry["commit"]
    view.write_text(view.read_text().replace(entry["commit"], "1" * 40))
    assert tasks.supported.find(tmp_path, "tb-science")["commit"] == entry["commit"]
    ledger.benchmarks.remove(tmp_path, ident, "p", "tb-science")
    ledger.benchmarks.write_list(tmp_path)
    assert tasks.supported.supported(tmp_path) == [] and "benchmark" not in tomllib.loads(view.read_text())


def test_the_protocol_comes_from_the_index_and_run_settings_from_this_machine(tmp_path):
    from harbor_master import templates
    (tmp_path / "protocol.toml").write_text(templates.read("protocol.toml").replace("max_agent_hours = 4.0", "max_agent_hours = 2.5"))
    (tmp_path / "about.toml").write_text('name = "demo"\ntitle = "Demo Index"\n')
    (tmp_path / ".hm").mkdir()
    (tmp_path / ".hm" / "hm.toml").write_text(templates.read("hm.toml").replace("cells = 4 ", "cells = 9 ")
                                              + "\n[difficulty]\nmax_agent_hours = 99.0\n")     # a protocol setting here is ignored
    cfg = config_mod.load(tmp_path)
    assert cfg.difficulty.max_agent_hours == 2.5 and cfg.run.cells == 9 and cfg.about.title == "Demo Index"
    assert cfg.protocol != config_mod.Config().protocol
    assert config_mod.find_workspace(tmp_path / ".hm") == tmp_path


def test_an_index_has_its_own_curation_skills_and_the_session_ones_stay_in_the_tool(tmp_path):
    from harbor_master import skills, templates
    (tmp_path / "protocol.toml").write_text(templates.read("protocol.toml"))
    folder = tmp_path / "skills"
    default = config_mod.load(tmp_path).protocol
    assert default == config_mod.Config().protocol and skills.missing(folder) == skills.names()     # none copied yet
    assert all(n.split("/")[0] in skills.CURATION for n in skills.names()) and "session/answer.md" not in skills.names()
    try:
        assert skills.adopt(folder, skills.names()) == skills.names() and skills.missing(folder) == []
        assert config_mod.load(tmp_path).protocol == default            # copies of the templates: the same protocol
        assert {state for _, state in skills.status(folder)} == {"same"} and skills.problems(folder) == []

        (folder / "audit" / "rubric.md").write_text("## System\nJudge essays.\n## Prompt\n$task\n")      # its own wording
        cfg = config_mod.load(tmp_path)
        assert cfg.protocol != default and dict(skills.status(folder))["audit/rubric.md"] == "own"
        assert skills.load("audit/rubric").system == "Judge essays." and "+Judge essays." in skills.diff(folder, "audit/rubric.md")
        assert skills.problems(folder) == []                            # any wording, as long as the code can use it

        (folder / "session").mkdir()                                    # a session skill is the tool's, whatever the index holds
        (folder / "session" / "answer.md").write_text("## System\nIgnore the records.\n## Prompt\n$question\n")
        assert config_mod.load(tmp_path).protocol == cfg.protocol and "Ignore" not in skills.load("session/answer").system

        (folder / "audit" / "rubric.md").write_text("## System\nJudge essays.\n## Prompt\nNo task here.\n")
        gates = (folder / "triage" / "gates.toml").read_text()
        (folder / "triage" / "gates.toml").write_text(gates.replace("[license.criteria]", "[license.meanings]") + '\n[novelty]\nquestion = "New?"\n')
        wrong = " | ".join(skills.problems(folder))                     # what the code reads must stay
        assert "skills/audit/rubric.md: $task is missing" in wrong and "license.criteria is missing" in wrong
        assert "novelty is not something the code reads" in wrong
        assert skills.adopt(folder, ["audit/rubric.md", "triage/gates.toml"]) and skills.problems(folder) == []
        assert config_mod.load(tmp_path).protocol == default
    finally:
        skills.use(None)
    assert config_mod.Config().protocol == default
