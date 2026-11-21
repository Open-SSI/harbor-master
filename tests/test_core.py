import tomllib

from harbor_master import ledger, tasks
from harbor_master.core import config as config_mod
from harbor_master.core import keys


def test_keys_are_scrubbed(ident, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "abcd1234efgh5678")
    out = keys.scrub("token abcd1234efgh5678 and sk-ant-REDACTEDREDACTEDREDACTED00")
    assert "abcd1234efgh5678" not in out and "sk-ant" not in out


def test_the_benchmark_list_comes_from_signed_records(tmp_path, ident):
    assert tasks.supported.supported(tmp_path) == []                            # a new index lists nothing
    entry = {"id": "tb-science", "name": "Terminal-Bench-Science", "version": "0.1.0", "released": "2026-08-26",
             "github": "https://github.com/harbor-framework/terminal-bench-science", "ref": "v0.1.0",
             "commit": "f81afac4f11048e77a15dfc8fb1dbfb897fea0ce", "tasks": "tasks", "description": "Science tasks in a terminal.", "paper": "none"}
    ledger.benchmarks.add(tmp_path, ident, "p", {k: v for k, v in entry.items() if k != "ref"})       # not pinned: it does not count
    assert tasks.supported.supported(tmp_path) == []
    ledger.benchmarks.add(tmp_path, ident, "p", entry)
    first = tasks.supported.supported(tmp_path)[0]
    assert (first["id"], first["released"], first["ref"], first["added_by"]) == ("tb-science", "2026-08-26", "v0.1.0", ident.github)
    assert tasks.supported.find(tmp_path, "TB-Science")["name"] == "Terminal-Bench-Science" and tasks.supported.find(tmp_path, "smdd-bench") is None
    assert not hasattr(config_mod, "roster") and not hasattr(config_mod, "RosterEntry")      # no roster: nothing is evaluated here

    ledger.benchmarks.write_list(tmp_path)                                      # one readable list, which changes nothing when edited
    view = tmp_path / "benchmarks.toml"
    listed = tomllib.loads(view.read_text())["benchmark"][0]
    assert listed["commit"] == entry["commit"] and listed["status"] == "listed"     # with where it stands
    view.write_text(view.read_text().replace(entry["commit"], "1" * 40))
    assert tasks.supported.find(tmp_path, "tb-science")["commit"] == entry["commit"]
    ledger.benchmarks.remove(tmp_path, ident, "p", "tb-science")
    ledger.benchmarks.write_list(tmp_path)
    assert tasks.supported.supported(tmp_path) == [] and "benchmark" not in tomllib.loads(view.read_text())
