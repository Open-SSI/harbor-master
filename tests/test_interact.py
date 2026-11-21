
from harbor_master import interact
from harbor_master.interact import Command


def test_commands_change_only_what_they_say(session):
    assert "Paused" in interact.execute(session, Command("pause")) and session.paused
    interact.execute(session, Command("undo"))
    assert not session.paused
    assert "There is no /budget command" in interact.execute(session, Command("budget", ["350"])) and not hasattr(session, "budget")
    assert "There is no /retry command" in interact.execute(session, Command("retry", ["t0001"]))      # no command that only says it is not built
