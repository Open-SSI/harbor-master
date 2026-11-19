"""An index is a repository of its own: started by its owner, joined by others, and guarded by the owner's lock."""
import subprocess

import typer
import pytest
from typer.testing import CliRunner

from harbor_master import ledger
from harbor_master.cli import app
from harbor_master.cli.common import check_lock
from harbor_master.core import config as config_mod
from harbor_master.core import identity

runner = CliRunner()


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
    act_as("bob")
    act_as("alice")
    assert hm("index", "new", "demo", tmp_path / "alice", "--title", "Demo Index").startswith("0 Started the Demo Index index")
    assert "started the shared index" in hm("index", "connect", remote, "-w", tmp_path / "alice")
    return act_as, tmp_path / "alice", tmp_path / "bob", remote


def test_a_new_index_is_a_repository_with_its_owner_and_nothing_of_the_machine(two):
    act_as, alice_ws, _, _ = two
    tracked = subprocess.run(["git", "ls-files"], cwd=alice_ws, capture_output=True, text=True).stdout.split()
    from harbor_master import skills
    assert sorted(tracked) == sorted([".gitignore", "README.md", "about.toml", "benchmarks.toml", "contributors.toml",
                                      "protocol.toml", "roster.toml", *(f"skills/{n}" for n in skills.names())])
    assert (alice_ws / ".hm" / "hm.toml").exists() and (alice_ws / ".hm" / "owner").read_text().startswith("alice ssh-ed25519 ")
    cfg = config_mod.load(alice_ws)
    assert (cfg.about.name, cfg.about.title) == ("demo", "Demo Index") and cfg.protocol == config_mod.Config().protocol
    shown = hm("index", "-w", alice_ws)
    assert "Demo Index" in shown and "Alice (@alice)" in shown and "not locked yet" in shown and "demo-index.git" in shown
    assert hm("index", "new", "Bad Name").startswith("1 ") and hm("benchmarks", "-w", alice_ws.parent).startswith("1 No index here")


def test_joining_clones_the_index_and_the_lock_guards_every_file_of_it(two):
    act_as, alice_ws, bob_ws, remote = two
    bob = act_as("bob")
    joined = hm("join", remote, bob_ws)
    assert joined.startswith("0 Demo Index") and "not on this index's contributor list yet" in joined
    assert (bob_ws / "protocol.toml").read_text() == (alice_ws / "protocol.toml").read_text()

    act_as("alice")                                               # the owner adds Bob, and locks
    with (alice_ws / "contributors.toml").open("a") as f:
        f.write(f'\n[[contributor]]\nname = "Bob"\ngithub = "bob"\nkey = "{bob.public_key}"\nfields = ["*"]\n')
    assert "Locked protocol" in hm("protocol", "lock", "-w", alice_ws)
    assert "already the locked ones" in hm("protocol", "lock", "-w", alice_ws)

    act_as("bob")
    assert "@bob" not in hm("contributors", "-w", bob_ws)        # his copy is from before
    assert hm("protocol", "lock", "-w", bob_ws).startswith("1 Only the owner")
    assert hm("sync", "-w", bob_ws).startswith("0 Synced")
    assert "@bob" in hm("contributors", "-w", bob_ws) and "They match" in hm("protocol", "-w", bob_ws)

    (bob_ws / "roster.toml").write_text('[[config]]\nname = "mine"\nagent = "a"\nmodel = "m"\n')     # Bob edits a locked file
    assert "roster.toml: not the locked copy" in hm("protocol", "-w", bob_ws)
    lock = ledger.protocol.current(ledger.Ledger(bob_ws).events)
    with pytest.raises(typer.Exit):
        check_lock(lock, config_mod.load(bob_ws), bob_ws)
    assert hm("sync", "-w", bob_ws).startswith("0 Synced")        # and a sync does not share his edit
    act_as("alice")
    assert hm("sync", "-w", alice_ws).startswith("0 Synced") and "mine" not in (alice_ws / "roster.toml").read_text()

    (alice_ws / "about.toml").write_text((alice_ws / "about.toml").read_text().replace('description = ""', 'description = "A demo."'))
    hm("sync", "-w", alice_ws)                                    # the description is not locked, and travels with a sync
    act_as("bob")
    subprocess.run(["git", "checkout", "--quiet", "--", "roster.toml"], cwd=bob_ws, check=True)
    assert hm("sync", "-w", bob_ws).startswith("0 Synced") and "A demo." in hm("index", "-w", bob_ws)
    check_lock(lock, config_mod.load(bob_ws), bob_ws)             # with the file put back, runs are allowed again


def test_a_changed_owner_or_an_old_tool_is_refused(two):
    act_as, alice_ws, bob_ws, remote = two
    bob = act_as("bob")
    hm("join", remote, bob_ws)
    listed = (bob_ws / "contributors.toml").read_text()
    (bob_ws / "contributors.toml").write_text(listed.split("[[contributor]]")[0] + f'[[contributor]]\nname = "Bob"\n'
                                              f'github = "bob"\nkey = "{bob.public_key}"\nrole = "owner"\n')
    refused = hm("protocol", "lock", "-w", bob_ws)                # Bob names himself the owner, and nothing works
    assert refused.startswith("1 Refused: the owner") and "joined under: alice" in refused and "listed now: bob" in refused
    (bob_ws / "contributors.toml").write_text(listed)
    assert hm("index", "-w", bob_ws).startswith("0 ")

    protocol = bob_ws / "protocol.toml"
    protocol.write_text(protocol.read_text().replace('harbor_master = "0.1.0"', 'harbor_master = "9.0.0"'))
    cfg = config_mod.load(bob_ws)
    assert cfg.protocol == config_mod.Config().protocol           # the version asked for is not part of the checksum
    with pytest.raises(typer.Exit):
        check_lock(None, cfg, bob_ws)


def test_a_listed_index_is_joined_by_name_and_held_to_its_entry(two, tmp_path):
    import tomllib
    from pathlib import Path
    from harbor_master.ledger import listing
    act_as, alice_ws, bob_ws, remote = two
    act_as("alice")
    printed = runner.invoke(app, ["index", "entry", "-w", str(alice_ws)]).output
    entry = tomllib.loads(printed.split("Add this to")[0])["index"][0]        # what the owner pastes into the list
    assert (entry["name"], entry["url"]) == ("demo", str(remote)) and entry["owner"].startswith("alice ssh-ed25519 ")
    listed = tmp_path / "indexes.toml"
    listed.write_text(printed.split("Add this to")[0])
    assert listing.read(listed.as_uri()) == listing.read(str(listed)) == [entry]      # from an address, or from a file

    shown = hm("indexes", "--list", listed)
    assert shown.startswith("0 ") and "demo" in shown and "@alice" in shown
    checked = hm("indexes", "--check", "--list", listed)
    assert checked.startswith("0 ") and "Demo Index" in checked and "sound" in checked

    act_as("bob")
    assert hm("join", "nothing", "--list", listed).startswith("1 nothing is not on the list of indexes (demo)")
    assert hm("join", "demo", bob_ws, "--list", listed).startswith("0 Demo Index")
    assert (bob_ws / ".hm" / "owner").read_text().strip() == entry["owner"]

    # The repository changes hands, or the entry is wrong: joining by name refuses, and keeps nothing.
    other = act_as("bob").public_key
    listed.write_text(f'[[index]]\nname = "demo"\nurl = "{remote}"\nowner = "bob {other}"\n')
    refused = hm("join", "demo", tmp_path / "again", "--list", listed)
    assert refused.startswith("1 Refused") and "names a different owner" in refused and not (tmp_path / "again").exists()
    assert "demo: the owner on its contributor list is not the owner on its entry" in hm("indexes", "--check", "--list", listed)

    listed.write_text(f'[[index]]\nname = "other"\nurl = "{remote}"\nowner = "{entry["owner"]}"\n'
                      f'[[index]]\nname = "other"\nurl = "{remote}/"\nowner = "x"\n[[index]]\nname = "Bad Name"\n')
    wrong = hm("indexes", "--check", "--list", listed)
    assert wrong.startswith("1 ") and "other: listed twice" in wrong and "its address is already listed" in wrong
    assert "Bad Name: missing url, owner" in wrong and "listed as other, but its about.toml says demo" in wrong
    assert hm("indexes", "--list", tmp_path / "missing.toml").startswith("1 Could not read the list of indexes")

    here = Path(__file__).parent.parent / "indexes.toml"                      # the real list, in this repository
    assert listing.problems(listing.read(str(here))) == []
    assert listing.DEFAULT == "https://raw.githubusercontent.com/Open-SSI/harbor-master/main/indexes.toml"
    assert listing.source("x") == "x" and listing.source() == listing.DEFAULT


def test_the_skills_are_the_indexes_own_and_the_lock_needs_them(two):
    act_as, alice_ws, bob_ws, remote = two
    shown = hm("skills", "-w", alice_ws)
    assert shown.startswith("0 ") and "skills/triage/gates.toml" in shown and "same as the template" in shown
    assert "all as the templates have them" in hm("index", "-w", alice_ws)

    rubric = alice_ws / "skills" / "audit" / "rubric.md"                  # the owner words one her own way
    rubric.write_text(rubric.read_text().replace("You audit a benchmark task", "You audit a robotics task"))
    assert "this index's own wording" in hm("skills", "-w", alice_ws) and "+You audit a robotics task" in hm("skills", "diff", "-w", alice_ws)
    assert "1 in its own wording" in hm("index", "-w", alice_ws)
    assert "Locked protocol" in hm("protocol", "lock", "-w", alice_ws)   # and her wording is locked with the rest

    act_as("bob")
    hm("join", remote, bob_ws)
    assert "robotics" in (bob_ws / "skills" / "audit" / "rubric.md").read_text() and "They match" in hm("protocol", "-w", bob_ws)
    assert hm("skills", "adopt", "audit/rubric", "-w", bob_ws).startswith("1 Only the owner")
    lock = ledger.protocol.current(ledger.Ledger(bob_ws).events)
    (bob_ws / "skills" / "rank" / "score.toml").unlink()                   # a skill gone from his copy: refused, not replaced by the tool's
    with pytest.raises(typer.Exit):
        check_lock(lock, config_mod.load(bob_ws), bob_ws)

    act_as("alice")
    (alice_ws / "skills" / "rank" / "score.toml").unlink()
    assert hm("protocol", "lock", "-w", alice_ws).startswith("1 Not locked: this index has no copy of 1")
    assert "skills/rank/score.toml" in hm("skills", "adopt", "-w", alice_ws)                 # with no name: only what is missing
    assert "robotics" in rubric.read_text()
    rubric.write_text("no sections, no task")
    assert hm("skills", "-w", alice_ws).startswith("1 ") and hm("protocol", "lock", "-w", alice_ws).startswith("1 Not locked: the code cannot use")
    assert "skills/audit/rubric.md" in hm("skills", "adopt", "audit/rubric", "-w", alice_ws)  # by name: the template again
    assert "Nothing differs" in hm("skills", "diff", "-w", alice_ws)
