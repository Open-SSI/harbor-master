"""An index is a repository of its own: started by its owner, joined by others, and guarded by the owner's lock."""
import subprocess
import tomllib
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from conftest import call
from harbor_master import ledger, skills, tasks
from harbor_master.cli import app, views
from harbor_master.cli.common import check_lock
from harbor_master.cli.setup import _describe, clone, connect, index_new
from harbor_master.core import config as config_mod
from harbor_master.core import identity
from harbor_master.ledger import listing

runner = CliRunner()


def synced(ws) -> bool:
    """A sync that went through, and said which index it was for."""
    out = hm("sync", "-w", ws)
    return out.startswith("0 Index: Demo Index") and "Synced." in out


def hm(*args: object) -> str:
    result = runner.invoke(app, [str(a) for a in args])
    return f"{result.exit_code} {' '.join(result.output.split())}"


@pytest.fixture
def two(tmp_path, monkeypatch):
    """Alice starts an index and shares it. Bob has an identity, and is not on its list."""
    people = {}

    def act_as(name: str) -> identity.Identity:
        monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / f"config-{name}"))
        if name not in people:
            people[name] = identity.create(name.title(), "Some University", f"{name}@example.org", name)
        return people[name]

    remote = tmp_path / "demo-index.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)], check=True)
    act_as("bob"), act_as("alice")
    assert call(index_new, "demo", tmp_path / "none").startswith("1 An index needs a store for its accepted tasks")
    made = call(index_new, "demo", tmp_path / "alice", "Demo Index", "", str(tmp_path / "store"))
    assert made.startswith("0 Started the Demo Index index") and "Created the task store" in made and (tmp_path / "store").is_dir()
    assert "started the shared index" in call(connect, tmp_path / "alice", str(remote))
    return act_as, tmp_path / "alice", tmp_path / "bob", remote


def test_a_new_index_is_a_repository_with_its_owner_and_nothing_of_the_machine(two):
    act_as, alice_ws, _, _ = two
    tracked = subprocess.run(["git", "ls-files"], cwd=alice_ws, capture_output=True, text=True).stdout.split()
    assert sorted(tracked) == sorted([".gitignore", "README.md", "about.toml", "contributors.toml", "ledger/_contributors/alice-manual.jsonl",
                                      "protocol.toml", "roster.toml", *(f"skills/{n}" for n in skills.names())])
    assert (alice_ws / ".hm" / "hm.toml").exists() and (alice_ws / ".hm" / "owner").read_text().startswith("alice ssh-ed25519 ")
    cfg = config_mod.load(alice_ws)
    assert (cfg.about.name, cfg.about.title) == ("demo", "Demo Index") and cfg.protocol == config_mod.Config().protocol
    shown = call(_describe, alice_ws)
    assert "Demo Index" in shown and "Alice (@alice)" in shown and "not locked yet" in shown and "demo-index.git" in shown
    assert call(index_new, "Bad Name").startswith("1 ") and "is not an index" in hm("benchmarks", "-w", alice_ws.parent)


def test_joining_clones_the_index_and_the_lock_guards_every_file_of_it(two):
    act_as, alice_ws, bob_ws, remote = two
    bob = act_as("bob")
    joined = call(clone, str(remote), bob_ws)
    assert joined.startswith("0 Demo Index") and "not on this index's contributor list yet" in joined
    assert (bob_ws / "protocol.toml").read_text() == (alice_ws / "protocol.toml").read_text()
    assert hm("contributors", "add", "@bob", bob.public_key, "-w", bob_ws).startswith("1 Only the owner adds a contributor")
    act_as("alice")                                               # the owner adds Bob with his key, and no lock is involved
    added = hm("contributors", "add", "@bob", bob.public_key, "--name", "Bob", "-w", alice_ws)
    assert added.startswith("0 @bob can contribute to this index now") and ledger.protocol.locks(ledger.Ledger(alice_ws).events) == []
    assert hm("contributors", "add", "carol", "not-a-key", "-w", alice_ws).startswith("1 That is not a public key")
    locked = hm("lock", "-w", alice_ws)                           # the settings and skills are locked as a version
    assert "Locked protocol v0.0.0" in locked and "Signed the contributor list" not in locked
    assert "registry" not in ledger.protocol.current(ledger.Ledger(alice_ws).events)       # the roster is not in it
    assert "is already the locked one" in hm("lock", "-w", alice_ws)

    act_as("bob")
    assert "@bob" not in hm("contributors", "-w", bob_ws)        # his copy is from before
    assert hm("lock", "-w", bob_ws).startswith("1 Only the owner")
    assert synced(bob_ws)
    assert "@bob" in hm("contributors", "-w", bob_ws) and "They match" in call(views.protocol, bob_ws)
    (bob_ws / "roster.toml").write_text('[[config]]\nname = "mine"\nagent = "a"\nmodel = "m"\n')     # the roster is not locked:
    lock = ledger.protocol.current(ledger.Ledger(bob_ws).events)
    check_lock(lock, config_mod.load(bob_ws), bob_ws)             # an edit to it refuses no run
    assert synced(bob_ws)        # and only its owner's sync shares one
    act_as("alice")
    assert synced(alice_ws) and "mine" not in (alice_ws / "roster.toml").read_text()
    (alice_ws / "about.toml").write_text((alice_ws / "about.toml").read_text().replace('description = ""', 'description = "A demo."'))
    (alice_ws / "roster.toml").write_text('[[config]]\nname = "new-model"\nagent = "a"\nmodel = "m"\n')    # a new model came out
    hm("sync", "-w", alice_ws)                                    # the description and the roster travel with the owner's sync
    act_as("bob")
    subprocess.run(["git", "checkout", "--quiet", "--", "roster.toml"], cwd=bob_ws, check=True)
    assert synced(bob_ws) and "A demo." in call(_describe, bob_ws) and "new-model" in (bob_ws / "roster.toml").read_text()
    check_lock(lock, config_mod.load(bob_ws), bob_ws)             # under the same lock


def test_a_changed_owner_or_an_old_tool_is_refused(two):
    act_as, alice_ws, bob_ws, remote = two
    bob = act_as("bob")
    call(clone, str(remote), bob_ws)
    listed = (bob_ws / "contributors.toml").read_text()
    (bob_ws / "contributors.toml").write_text(listed.split("[[contributor]]")[0] + f'[[contributor]]\nname = "Bob"\n'
                                              f'github = "bob"\nkey = "{bob.public_key}"\nrole = "owner"\n')
    refused = hm("lock", "-w", bob_ws)                            # Bob names himself the owner, and nothing works
    assert refused.startswith("1 Refused: the owner") and "joined under: alice" in refused and "listed now: bob" in refused
    (bob_ws / "contributors.toml").write_text(listed)
    assert hm("contributors", "-w", bob_ws).startswith("0 ")
    protocol = bob_ws / "protocol.toml"
    protocol.write_text(protocol.read_text().replace('harbor_master = "0.1.0"', 'harbor_master = "9.0.0"'))
    cfg = config_mod.load(bob_ws)
    assert cfg.protocol == config_mod.Config().protocol           # the version asked for is not part of the checksum
    with pytest.raises(typer.Exit):
        check_lock(None, cfg, bob_ws)


def test_a_listed_index_is_joined_by_name_and_held_to_its_entry(two, tmp_path):
    act_as, alice_ws, bob_ws, remote = two
    act_as("alice")
    printed = runner.invoke(app, ["entry", "-w", str(alice_ws)]).output.split("Add this to")[0]
    entry = tomllib.loads(printed)["index"][0]                                # what the owner pastes into the list
    assert (entry["name"], entry["url"]) == ("demo", str(remote)) and entry["owner"].startswith("alice ssh-ed25519 ")
    listed = tmp_path / "indexes.toml"
    listed.write_text(printed)
    assert listing.read(listed.as_uri()) == listing.read(str(listed)) == [entry]      # from an address, or from a file
    shown = hm("indexes", "--list", listed)
    assert shown.startswith("0 ") and "demo" in shown and "@alice" in shown
    checked = hm("indexes", "--check", "--list", listed)
    assert checked.startswith("0 ") and "Demo Index" in checked and "sound" in checked

    act_as("bob")
    assert call(clone, "nothing", None, str(listed)).startswith("1 nothing is not on the list of indexes (demo)")
    assert call(clone, "demo", bob_ws, str(listed)).startswith("0 Demo Index")
    assert (bob_ws / ".hm" / "owner").read_text().strip() == entry["owner"]

    # The repository changes hands, or the entry is wrong: joining by name refuses, and keeps nothing.
    listed.write_text(f'[[index]]\nname = "demo"\nurl = "{remote}"\nowner = "bob {act_as("bob").public_key}"\n')
    refused = call(clone, "demo", tmp_path / "again", str(listed))
    assert refused.startswith("1 Refused") and "names a different owner" in refused and not (tmp_path / "again").exists()
    assert "demo: the owner on its contributor list is not the owner on its entry" in hm("indexes", "--check", "--list", listed)
    listed.write_text(f'[[index]]\nname = "other"\nurl = "{remote}"\nowner = "{entry["owner"]}"\n'
                      f'[[index]]\nname = "other"\nurl = "{remote}/"\nowner = "x"\n[[index]]\nname = "Bad Name"\n')
    wrong = hm("indexes", "--check", "--list", listed)
    assert wrong.startswith("1 ") and "other: listed twice" in wrong and "its address is already listed" in wrong
    assert "Bad Name: missing url, owner" in wrong and "listed as other, but its about.toml says demo" in wrong
    assert hm("indexes", "--list", tmp_path / "missing.toml").startswith("1 Could not read the list of indexes")

    assert listing.problems(listing.read(str(Path(__file__).parent.parent / "indexes.toml"))) == []      # the real list, in this repository
    assert listing.DEFAULT == "https://raw.githubusercontent.com/Open-SSI/harbor-master/main/indexes.toml"
    assert listing.source("x") == "x" and listing.source() == listing.DEFAULT


def test_the_skills_are_the_indexes_own_and_the_lock_copies_in_what_is_missing(two):
    act_as, alice_ws, bob_ws, remote = two
    shown = call(views.skills, alice_ws)
    assert shown.startswith("0 ") and "skills/triage/gates.toml" in shown and "same as the template" in shown
    assert "all as the templates have them" in call(_describe, alice_ws)
    rubric = alice_ws / "skills" / "audit" / "rubric.md"                  # the owner words one her own way
    rubric.write_text(rubric.read_text().replace("You audit a benchmark task", "You audit a robotics task"))
    assert "this index's own wording" in call(views.skills, alice_ws)
    assert "1 in its own wording" in call(_describe, alice_ws)
    assert "Locked protocol" in hm("lock", "-w", alice_ws)                # and her wording is locked with the rest

    act_as("bob")
    call(clone, str(remote), bob_ws)
    assert "robotics" in (bob_ws / "skills" / "audit" / "rubric.md").read_text() and "They match" in call(views.protocol, bob_ws)
    lock = ledger.protocol.current(ledger.Ledger(bob_ws).events)
    (bob_ws / "skills" / "rank" / "score.toml").unlink()                   # a skill gone from his copy: refused, not replaced by the tool's
    with pytest.raises(typer.Exit):
        check_lock(lock, config_mod.load(bob_ws), bob_ws)

    act_as("alice")
    (alice_ws / "skills" / "rank" / "score.toml").unlink()
    locked = hm("lock", "-w", alice_ws)                                    # the owner's lock copies the template in
    assert locked.startswith("0 Copied 1 skill") and "Locked protocol" in locked and (alice_ws / "skills" / "rank" / "score.toml").exists()
    assert "robotics" in rubric.read_text()
    rubric.write_text("no sections, no task")
    assert hm("lock", "-w", alice_ws).startswith("1 ") and "Not locked: the code cannot use" in hm("lock", "-w", alice_ws)


def test_anyone_who_can_curate_adds_a_benchmark_with_no_step_by_the_owner(two, tmp_path, monkeypatch):
    act_as, alice_ws, bob_ws, remote = two
    repo = tmp_path / "toy-bench"
    (repo / "tasks" / "one").mkdir(parents=True)
    (repo / "tasks" / "one" / "task.toml").write_text('version = "1.0"\n')
    (repo / "README.md").write_text("# Toy\n\nTasks that add small numbers.\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=a", "-c", "user.email=a@b.c", "commit", "-q", "-m", "one"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    assert "Locked protocol" in hm("lock", "-w", alice_ws)
    bob = act_as("bob")
    call(clone, str(remote), bob_ws)
    assert hm("benchmarks", "add", repo, "--no-agent", "-w", bob_ws).startswith("1 @bob is not on this index's contributor list")
    act_as("alice")
    assert hm("contributors", "add", "bob", bob.public_key, "-w", alice_ws).startswith("0 ")
    locks = len(ledger.protocol.locks(ledger.Ledger(alice_ws).events))
    assert locks == 1                                                                       # adding a person made no new lock
    act_as("bob")
    hm("sync", "-w", bob_ws)
    drafted = hm("benchmarks", "add", repo, "--no-agent", "-w", bob_ws)                     # pinned to the branch: it has no tag
    assert drafted.startswith("0 ") and "Tasks that add small numbers." in drafted and "Still needed: paper" in drafted
    assert hm("benchmarks", "confirm", "-w", bob_ws).startswith("1 Not added yet: it still needs paper")
    assert "Add it, signed with your key: hm benchmarks confirm" in hm("benchmarks", "revise", "paper: none", "-w", bob_ws)
    added = hm("benchmarks", "confirm", "-w", bob_ws)
    assert added.startswith("0 Added toy-bench") and "signed by @bob and shared" in added

    act_as("alice")                                                                         # it reaches the owner by a sync, and that is all
    hm("sync", "-w", alice_ws)
    entry = tasks.supported.find(alice_ws, "toy-bench")
    assert (entry["added_by"], entry["ref"], entry["paper"]) == ("bob", "main", "none")
    listed = lambda ws: [b["id"] for b in tomllib.loads((ws / "benchmarks.toml").read_text()).get("benchmark", [])]  # noqa: E731
    assert listed(alice_ws) == ["toy-bench"] and ledger.verify(alice_ws) == []
    again = hm("benchmarks", "add", str(repo) + "/", "--id", "other-name", "--no-agent", "-w", alice_ws)      # the same repository, under any name
    assert again.startswith("1 That repository is on this index's list already, as toy-bench, added by @bob")
    assert len(ledger.protocol.locks(ledger.Ledger(alice_ws).events)) == locks              # no new lock, and the old one still holds
    check_lock(ledger.protocol.current(ledger.Ledger(alice_ws).events), config_mod.load(alice_ws), alice_ws)

    carol = act_as("carol")                                                                 # only the adder or the owner takes it off
    act_as("alice")
    hm("contributors", "add", "carol", carol.public_key, "-w", alice_ws)
    act_as("carol")
    assert "Only they, or the owner" in hm("benchmarks", "remove", "toy-bench", "-w", alice_ws)
    act_as("alice")
    assert hm("benchmarks", "remove", "toy-bench", "-w", alice_ws).startswith("0 Took toy-bench off")
    assert tasks.supported.supported(alice_ws) == [] and listed(alice_ws) == []

    entry = {**entry, "added_by": "", "added": ""}                                          # two people add at the same moment:
    for who, ws, name in (("alice", alice_ws, "one"), ("bob", bob_ws, "two")):              # each commits the list with their own in it
        me = act_as(who)
        if who == "bob":
            hm("sync", "-w", bob_ws)
            subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=bob_ws, check=True)     # as it was before Alice's reached him
        ledger.benchmarks.add(ws, me, "p", {**entry, "id": name, "github": f"https://example.org/{name}"})
        ledger.Sync(ws, who, who)._commit(f"add {name}")
        if who == "alice":
            hm("sync", "-w", alice_ws)
    assert hm("sync", "-w", bob_ws).startswith("0 ")                                        # the one list merges by itself
    act_as("alice")
    hm("sync", "-w", alice_ws)
    assert listed(bob_ws) == ["one", "two"] == listed(alice_ws)
    assert subprocess.run(["git", "status", "--porcelain"], cwd=bob_ws, capture_output=True, text=True).stdout == ""


def test_each_lock_has_a_version_and_the_contributor_list_is_the_owners_to_change(two):
    act_as, alice_ws, bob_ws, remote = two
    version = lambda: ledger.protocol.version(ledger.protocol.current(ledger.Ledger(alice_ws).events))  # noqa: E731
    assert "Locked protocol v0.0.0" in hm("lock", "-w", alice_ws)          # everything starts at 0.0.0
    (alice_ws / "roster.toml").write_text('[[config]]\nname = "mine"\nagent = "a"\nmodel = "m"\n')
    assert "is already the locked one" in hm("lock", "-w", alice_ws)       # the roster is not locked, so nothing changed
    assert "Locked protocol v0.0.1" in hm("lock", "patch", "-w", alice_ws) and version() == "0.0.1"
    protocol = alice_ws / "protocol.toml"
    protocol.write_text(protocol.read_text().replace("max_agent_hours = 4.0", "max_agent_hours = 2.0"))
    changed = hm("lock", "minor", "-w", alice_ws)                 # changed settings: a new minor version
    assert "Locked protocol v0.1.0" in changed and "It replaces" in changed and version() == "0.1.0"
    assert "Locked: v0.1.0" in call(views.protocol, alice_ws) and "locked as v0.1.0" in call(_describe, alice_ws)
    assert "is already the locked one" in hm("lock", "-w", alice_ws) and version() == "0.1.0"
    refused = hm("lock", "major", "-w", alice_ws)                 # 1.0.0 is for the release, and cannot be set yet
    assert refused.startswith("1 ") and "1.0.0 is the index's release: hm release sets it" in refused and version() == "0.1.0"
    assert ledger.protocol.bumped("0.0.1", "patch") == "0.0.2" and ledger.protocol.bumped("0.1.4", "minor") == "0.2.0"

    bob = act_as("bob")                                           # someone adds himself to the list by hand
    call(clone, str(remote), bob_ws)
    by_hand = lambda ws: (ws / "contributors.toml").write_text(                                         # noqa: E731
        (ws / "contributors.toml").read_text() + f'\n[[contributor]]\nname = "Bob"\ngithub = "bob"\nkey = "{bob.public_key}"\n')
    by_hand(bob_ws)
    with pytest.raises(typer.Exit):
        check_lock(ledger.protocol.current(ledger.Ledger(bob_ws).events), config_mod.load(bob_ws), bob_ws)
    assert any("not the list its owner signed" in p for p in ledger.verify(bob_ws))
    act_as("alice")                                               # the owner's own edit by hand is signed at her next lock
    by_hand(alice_ws)
    signed = hm("lock", "-w", alice_ws)
    assert "Signed the contributor list" in signed and "is already the locked one" in signed and version() == "0.1.0"
    assert ledger.verify(alice_ws) == []


def test_four_commands_work_anywhere_and_the_rest_inside_an_index_folder(tmp_path, monkeypatch, ident, capsys):
    import subprocess

    from harbor_master import cli
    assert cli.ANYWHERE == ("init", "new", "open", "indexes")
    monkeypatch.chdir(tmp_path)
    assert call(index_new, "one", None, "First Index", "", str(tmp_path / "store1")).startswith("0")

    def typed(*args):                                   # as in a shell
        monkeypatch.setattr("sys.argv", ["hm", *args])
        try:
            cli.main()
            code = 0
        except SystemExit as stop:
            code = stop.code or 0
        return code, " ".join(capsys.readouterr().out.split())

    code, said = typed()                                # hm alone does nothing, and says what there is
    assert code == 2 and "hm init" in said and "hm open INDEX" in said and "hm new INDEX --tasks ORG/NAME --repo OWNER/NAME" in said
    for name in ("todo", "lock", "contributors", "cut", "sync", "benchmarks"):
        code, said = typed(name)
        assert code == 2 and f"hm {name} works inside an index's folder" in said and "hm open INDEX" in said
    assert all(typed(name, "--help")[0] == 0 for name in cli.ANYWHERE)
    assert hm("todo").startswith("1 This folder is not inside an index") and "Name the index: hm open INDEX" in hm("open")
    assert hm("todo", "-w", tmp_path / "one").startswith("0 Index: First Index")
    for gone in ("status", "leaderboard", "stats", "roster", "skills", "whoami", "join", "index", "keys", "ledger", "tasks", "adapt"):
        assert hm(gone, "-w", tmp_path / "one").startswith("2 ")          # no hidden commands are left

    plain = tmp_path / "plain"                          # a repository that looks like an index and has no canary is not cloned
    plain.mkdir()
    (plain / "protocol.toml").write_text((tmp_path / "one" / "protocol.toml").read_text())
    (plain / "about.toml").write_text('name = "plain"\n')
    for args in ("init -q -b main", "add -A", "-c user.name=a -c user.email=a@b.c commit -q -m one"):
        subprocess.run(["git", *args.split()], cwd=plain, check=True)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    out = hm("open", f"file://{plain}")
    assert out.startswith("1") and "is not an index, because its about.toml has no canary line" in out
    assert not list((tmp_path / "elsewhere").iterdir())                                   # nothing of it was kept
    monkeypatch.chdir(tmp_path)

    monkeypatch.chdir(tmp_path / "one")
    assert typed("contributors")[0] == 0 and hm("todo").startswith("0 Index: First Index")      # inside one: that one
    assert "dashboard needs a terminal" in hm("open")                                     # its owner's dashboard, which draws on a terminal
    assert "not shared yet" in hm("sync") and "git remote add origin URL, then hm sync" in hm("sync")
