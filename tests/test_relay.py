"""The relay never waits for a view: a terminal that stops reading must not stop the run behind it, nor be closed for it."""
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from harbor_master import host

# Stands in for the run: it fills its screen as fast as it can, and notes the time after each write. Were the relay to
# wait for a view that does not read, its writes would block and the times would stop. It also counts the times it is
# told its size changed, which is how the relay has a screen draw itself again in full.
FLOOD = textwrap.dedent("""
    import os, signal, sys, time
    beat, end = sys.argv[1], time.time() + float(sys.argv[2])
    def resized(*_):
        with open(beat + ".size", "a") as f:
            f.write(str(time.time()) + "\\n")
    signal.signal(signal.SIGWINCH, resized)
    line = b"x" * 65535 + b"\\n"
    while time.time() < end:
        os.write(1, line)
        with open(beat + ".new", "w") as f:
            f.write(str(time.time()))
        os.replace(beat + ".new", beat)     # whole, never half written when it is read
""")

RELAY = textwrap.dedent("""
    import os, sys
    from pathlib import Path
    from harbor_master import host
    ws, flood, beat, seconds = sys.argv[1:5]
    real = os.execv     # the run is replaced by the flood: the same call, another program
    host.os.execv = lambda exe, args: real(sys.executable, [sys.executable, flood, beat, seconds])
    host.relay(Path(ws), [], "test", False)
""")


def test_a_view_that_stops_reading_drops_what_it_missed_and_is_drawn_afresh_when_it_reads_again(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    ws, flood, beat = tmp_path / "index", tmp_path / "flood.py", tmp_path / "beat"
    ws.mkdir()
    flood.write_text(FLOOD)
    seconds = 8.0
    relay = subprocess.Popen([sys.executable, "-c", RELAY, str(ws), str(flood), str(beat), str(seconds)],
                             env={**os.environ, "XDG_RUNTIME_DIR": str(tmp_path / "run")})
    try:
        view_path, _ = host._sockets(ws)
        for _ in range(200):
            if os.path.exists(view_path):
                break
            time.sleep(0.02)
        stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stalled.connect(view_path)                  # a view whose terminal stops reading
        reading = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        reading.connect(view_path)                  # and one that keeps on
        reading.settimeout(0.2)
        got, until = 0, time.time() + 3.0
        while time.time() < until:
            try:
                got += len(reading.recv(1 << 20))
            except TimeoutError:
                pass
        assert time.time() - float(beat.read_text()) < 1.0     # the run is still writing
        assert got > 8 * host.MAX_BACKLOG                        # the reading view kept getting the screen
        redrawn = Path(str(beat) + ".size")
        before = len(redrawn.read_text().split()) if redrawn.exists() else 0
        stalled.settimeout(0.5)                     # the stalled view reads again
        taken, seen = 0, b""
        until = time.time() + 2.0
        while time.time() < until:
            try:
                chunk = stalled.recv(1 << 20)
            except TimeoutError:
                continue
            assert chunk, "a view is never closed for being slow"
            if host.RESYNC not in seen:
                seen = (seen + chunk)[-(1 << 21):]
            taken += len(chunk)
        assert host.RESYNC in seen                  # a clean start, after what it missed was dropped
        assert taken > host.MAX_BACKLOG             # and it gets the screen again
        time.sleep(0.3)
        assert len(redrawn.read_text().split()) >= before + 2      # drawn afresh: a size it does not have, then its own
    finally:
        relay.wait(timeout=seconds + 10)
    assert relay.returncode == 0
