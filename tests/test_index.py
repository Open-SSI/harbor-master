"""An index is a repository of its own: started by its owner, joined by others, and guarded by the owner's lock."""
import subprocess
import tomllib

import pytest
from typer.testing import CliRunner

from conftest import call
from harbor_master import ledger, tasks
from harbor_master.cli import app
from harbor_master.cli.common import check_index
from harbor_master.cli.setup import clone, connect, index_new
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
    act_as("bob"), act_as("alice")
    assert call(index_new, "demo", tmp_path / "none").startswith("1 An index needs a store for its accepted tasks")
    made = call(index_new, "demo", tmp_path / "alice", "Demo Index", "", str(tmp_path / "store"))
    assert made.startswith("0 Started the Demo Index index") and "Created the task store" in made and (tmp_path / "store").is_dir()
    assert "started the shared index" in call(connect, tmp_path / "alice", str(remote))
    return act_as, tmp_path / "alice", tmp_path / "bob", remote


def test_anyone_who_can_curate_adds_a_benchmark_with_no_step_by_the_owner(two, tmp_path, monkeypatch):
    act_as, alice_ws, bob_ws, remote = two
    repo = tmp_path / "toy-bench"
    (repo / "tasks" / "one").mkdir(parents=True)
    (repo / "tasks" / "one" / "task.toml").write_text('version = "1.0"\n')
    for part, file in (("tests", "test.sh"), ("solution", "solve.sh")):     # tested, and solved: its checks pass
        (repo / "tasks" / "one" / part).mkdir()
        (repo / "tasks" / "one" / part / file).write_text("exit 0\n")
    (repo / "README.md").write_text("# Toy\n\nTasks that add small numbers.\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=a", "-c", "user.email=a@b.c", "commit", "-q", "-m", "one"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    bob = act_as("bob")
    call(clone, str(remote), bob_ws)
    shared = subprocess.run(["git", "ls-files"], cwd=bob_ws, capture_output=True, text=True).stdout.split()
    assert {"hm.example.toml", "secrets.env.example"} <= set(shared) and not {"hm.toml", "secrets.env"} & set(shared)
    assert (bob_ws / "hm.toml").is_file() and (bob_ws / "secrets.env").stat().st_mode & 0o077 == 0     # the clone's own, private
    assert hm("benchmarks", "add", repo, "--no-agent", "-w", bob_ws).startswith("1 @bob is not on this index's contributor list")
    act_as("alice")
    assert hm("contributors", "add", "bob", bob.public_key, "-w", alice_ws).startswith("0 ")
    act_as("bob")
    hm("benchmarks", "-w", bob_ws)
    drafted = hm("benchmarks", "add", repo, "--no-agent", "-w", bob_ws)                     # pinned to the branch: it has no tag
    assert drafted.startswith("0 ") and "Tasks that add small numbers." in drafted and "Still needed: paper" in drafted
    assert hm("benchmarks", "confirm", "-w", bob_ws).startswith("1 Not added yet: it still needs paper")
    assert "Add it, signed with your key: hm benchmarks confirm" in hm("benchmarks", "revise", "paper: none", "-w", bob_ws)
    added = hm("benchmarks", "confirm", "-w", bob_ws)
    assert added.startswith("0 ") and "Added toy-bench" in added and "signed by @bob and shared" in added

    act_as("alice")                                                                         # it reaches the owner by their next command, and that is all
    hm("benchmarks", "-w", alice_ws)
    entry = tasks.supported.find(alice_ws, "toy-bench")
    assert (entry["added_by"], entry["ref"], entry["paper"]) == ("bob", "main", "none")
    listed = lambda ws: [b["id"] for b in tomllib.loads((ws / "benchmarks.toml").read_text()).get("benchmark", [])]  # noqa: E731
    assert listed(alice_ws) == ["toy-bench"] and ledger.verify(alice_ws) == []
    again = hm("benchmarks", "add", str(repo) + "/", "--id", "other-name", "--no-agent", "-w", alice_ws)      # the same repository, under any name
    assert again.startswith("1 That repository is on this index's list already, as toy-bench, added by @bob")
    check_index(config_mod.load(alice_ws), alice_ws)

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
            hm("benchmarks", "-w", bob_ws)
            subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=bob_ws, check=True)     # as it was before Alice's reached him
        ledger.benchmarks.add(ws, me, "p", {**entry, "id": name, "github": f"https://example.org/{name}"})
        ledger.Sync(ws, who, who)._commit(f"add {name}")
        if who == "alice":
            hm("benchmarks", "-w", alice_ws)
    assert hm("benchmarks", "-w", bob_ws).startswith("0 ")                                        # the one list merges by itself
    act_as("alice")
    hm("benchmarks", "-w", alice_ws)
    assert listed(bob_ws) == ["one", "two"] == listed(alice_ws)
    assert subprocess.run(["git", "status", "--porcelain"], cwd=bob_ws, capture_output=True, text=True).stdout == ""


def test_four_commands_work_anywhere_and_the_rest_inside_an_index_folder(tmp_path, monkeypatch, ident, capsys):
    import subprocess

    from harbor_master import cli
    assert cli.ANYWHERE == ("init", "new", "clone", "indexes")
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
    assert code == 2 and "hm init" in said and "hm clone INDEX" in said and "hm new INDEX --tasks ORG/NAME --repo OWNER/NAME" in said
    for name in ("contributors", "cut", "benchmarks", "view", "publish"):
        code, said = typed(name)
        assert code == 2 and f"hm {name} works inside an index's folder" in said and "hm clone INDEX" in said
    assert all(typed(name, "--help")[0] == 0 for name in cli.ANYWHERE)
    assert hm("benchmarks").startswith("1 This folder is not inside an index") and hm("view").startswith("1 This folder is not inside an index")
    assert hm("benchmarks", "-w", tmp_path / "one").startswith("0 ") and "A run works only on these" in hm("benchmarks", "-w", tmp_path / "one")
    for gone in ("status", "leaderboard", "stats", "roster", "skills", "whoami", "join", "index", "keys", "ledger", "tasks", "adapt",
                 "open", "entry", "lock", "eval", "todo", "release", "sync"):
        assert hm(gone, "-w", tmp_path / "one").startswith("2 ")          # no hidden commands are left

    plain = tmp_path / "plain"                          # a repository that looks like an index and has no canary is not cloned
    plain.mkdir()
    (plain / "protocol.toml").write_text((tmp_path / "one" / "protocol.toml").read_text())
    (plain / "about.toml").write_text('name = "plain"\n')
    for args in ("init -q -b main", "add -A", "-c user.name=a -c user.email=a@b.c commit -q -m one"):
        subprocess.run(["git", *args.split()], cwd=plain, check=True)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    out = hm("clone", f"file://{plain}")
    assert out.startswith("1") and "is not an index, because its about.toml has no canary line" in out
    assert not list((tmp_path / "elsewhere").iterdir())                                   # nothing of it was kept
    monkeypatch.chdir(tmp_path)

    monkeypatch.chdir(tmp_path / "one")
    assert typed("contributors")[0] == 0 and hm("benchmarks").startswith("0 ")       # inside one: that one
    assert "dashboard needs a terminal" in hm("view")                                     # its owner's dashboard, which draws on a terminal
    assert "Not in step" not in hm("benchmarks")                                          # an index on this machine only: nothing to pull, no complaint
