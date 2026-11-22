"""A ledger file edited by hand stops hm with the file and the line, and how to put it back: never a traceback."""
import pytest

from harbor_master import ledger
from harbor_master.ledger.store import BrokenRecord


async def test_a_line_that_is_not_a_record_names_its_file_and_line(workspace, session):
    await session.run()
    shard = next((workspace / "ledger" / "demo-bench").glob("*.jsonl"))
    lines = shard.read_text().splitlines()
    shard.write_text("\n".join([lines[0], "2" + lines[1], *lines[2:]]) + "\n")     # a key pressed in an open editor
    with pytest.raises(BrokenRecord, match=r"line 2, is not a record .*git checkout -- "):
        ledger.Ledger(workspace)
