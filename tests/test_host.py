"""A run outlives its terminal: the view that shows it must leave the terminal as it found it."""
from harbor_master import host


def test_a_view_does_not_leave_the_other_screen_twice():
    """A run's screen that ends leaves the other screen itself and prints its summary on the first one. The view then
    must not leave it again: that moves the cursor back up, and the next line is written over the summary."""
    back, tail = host.back_already(False, b"", b"\x1b[?1049h\x1b[Hdrawing")
    assert not back
    back, tail = host.back_already(back, tail, b"more drawing")                 # no mark: nothing changes
    assert not back
    back, tail = host.back_already(back, tail, b"bye\x1b[?10")                  # a mark split between two reads
    back, tail = host.back_already(back, tail, b"49l\n summary table")
    assert back
    back, tail = host.back_already(back, tail, b"Run 1: .hm/runs/1")
    assert back
    assert not host.LEAVE.removesuffix(host.BACK).endswith(host.BACK)
