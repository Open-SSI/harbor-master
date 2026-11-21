"""hm init: the line a person sends an index's owner (DESIGN.md Section 11)."""


def test_the_line_a_person_sends_carries_who_they_are_and_the_index_lists_it(tmp_path, monkeypatch, ident):
    import tomllib

    from typer.testing import CliRunner

    from harbor_master import ledger
    from harbor_master.cli import app
    monkeypatch.chdir(tmp_path)
    hm = lambda *a: CliRunner().invoke(app, [str(x) for x in a])      # noqa: E731
    import subprocess
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(tmp_path / "mine.git")], check=True)
    assert hm("new", "mine", "--tasks", tmp_path / "store", "--repo", tmp_path / "mine.git").exit_code == 0
    ws = tmp_path / "mine"
    listed = lambda: {c["github"]: c for c in tomllib.loads((ws / ledger.CONTRIBUTORS).read_text())["contributor"]}     # noqa: E731
    assert listed()[ident.github]["email"] == ident.email                 # the owner's own, from hm init
    import shlex
    assert shlex.split(ident.line()) == ["hm", "contributors", "add", ident.github, ident.public_key, "--name", ident.name,
                                         "--affiliation", ident.affiliation, "--email", ident.email]

    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBobBobBobBobBobBobBobBobBobBobBobBobBobBobBobB"
    theirs = f"hm contributors add bob '{key}' --name 'Bob Builder' --affiliation 'Brick Lab' --email bob@example.org"
    monkeypatch.chdir(ws)
    out = hm(*shlex.split(theirs)[1:])                                    # pasted in a shell as it is, inside the index's folder
    assert out.exit_code == 0, out.output
    bob = listed()["bob"]
    assert (bob["name"], bob["affiliation"], bob["email"]) == ("Bob Builder", "Brick Lab", "bob@example.org")
    assert bob["key"] == key and ledger.verify(ws) == []
    assert hm("--version").output.strip().startswith("hm ") and "hm init" in " ".join(hm("-h").output.split())

    assert "cd out of it first" in hm("new", "theirs", "--tasks", tmp_path / "store2", "--repo", "a/b").output
    monkeypatch.chdir(tmp_path)
    refused = hm("new", "theirs", "--tasks", tmp_path / "store2", "--repo", tmp_path / "nowhere.git")   # checked before anything is made
    told = " ".join(refused.output.split())
    assert refused.exit_code == 1 and "Nothing was made" in told and "nowhere.git could not be read: fatal:" in told
    assert not (tmp_path / "theirs").exists() and not (tmp_path / "store2").exists()
    assert "is not empty" in " ".join(hm("new", "theirs", "--tasks", tmp_path / "store2", "--repo", tmp_path / "mine.git").output.split())
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(tmp_path / "theirs.git")], check=True)
    made = hm("new", "theirs", "--tasks", tmp_path / "store2", "--repo", tmp_path / "theirs.git")      # hm new is a plain command: no boxes
    assert made.exit_code == 0 and (tmp_path / "theirs" / "about.toml").is_file() and "cd theirs" in made.output and "your next hm command shares them" in made.output
    assert "roster" not in made.output
    assert hm("new", "x").exit_code == 2                                  # what it needs is on its line
