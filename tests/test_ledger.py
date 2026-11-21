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
