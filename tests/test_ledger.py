import json

from harbor_master import ledger


def listed(tmp_path, ident, on_the_list=True):
    """A ledger folder, with the test's runner on its contributor list unless told otherwise."""
    d = tmp_path / "ledger"
    d.mkdir()
    if on_the_list:
        (d / ledger.CONTRIBUTORS).write_text(f'[[contributor]]\nname="T"\ngithub="{ident.github}"\nkey="{ident.public_key}"\n')
    return d


def trial(shard, index, passed=False, agent="a", **more):
    shard.append("trial", "x/t1", "dig", purpose="difficulty", agent=agent, model="m", trial_index=index, passed=passed, **more)


def test_shard_chain_signature_and_tampering(tmp_path, ident):
    d = listed(tmp_path, ident)
    shard = ledger.Shard(d, "x", ident, "r1", "p1")
    for i in range(3):
        trial(shard, i)
    assert ledger.verify(d) == []
    lines = shard.path.read_text().splitlines()
    shard.path.write_text("\n".join([lines[0], json.dumps({**json.loads(lines[1]), "passed": True}), lines[2]]) + "\n")
    assert any("edited after signing" in p for p in ledger.verify(d))


def test_a_filled_slot_is_rejected(tmp_path, ident):
    d = listed(tmp_path, ident)
    for run in ("r1", "r2"):
        trial(ledger.Shard(d, "x", ident, run, "p1"), 0, passed=True)
    assert any("already recorded" in p for p in ledger.verify(d))


def test_unlisted_runner_and_shared_handles_are_rejected(tmp_path, ident):
    d = listed(tmp_path, ident, on_the_list=False)
    ledger.Shard(d, "x", ident, "r1", "p1").append("submission", "x/t1", "dig")
    assert any("not on the contributor list" in p for p in ledger.verify(d))
    people = [{"name": "A", "github": "same", "key": "k1"}, {"name": "A", "github": "same", "key": "k2"}]
    assert any("share a github" in p for p in ledger.check_contributors(people))


def test_a_trial_that_ran_a_different_build_than_its_pin_is_rejected(tmp_path, ident):
    d = listed(tmp_path, ident)
    shard = ledger.Shard(d, "x", ident, "r1", "p1")
    trial(shard, 0, agent="claude-code", agent_version="2.1.292", agent_version_ran="2.1.292")
    assert ledger.verify(d) == []
    trial(shard, 1, agent="claude-code", agent_version="2.1.292", agent_version_ran="2.1.300")
    assert any("pinned to claude-code 2.1.292 but 2.1.300 ran" in p for p in ledger.verify(d))


def test_only_people_with_a_handle_and_a_key_can_contribute():
    people = [{"name": "Yibo Wen", "affiliation": "NU", "github": "Yibo-Wen", "key": "ssh-ed25519 AAA", "role": "owner"},
              {"name": "Chenwei Wu", "affiliation": "UM", "github": "", "key": ""}]
    assert ledger.check_contributors(people) == []          # entries still waiting for a key are not an error
    assert [c["name"] for c in ledger.active(people)] == ["Yibo Wen"]
    assert ledger.status(people[1]).startswith("listed, waiting")
    assert ledger.owner_keys(people) == ["Yibo-Wen ssh-ed25519 AAA"]
