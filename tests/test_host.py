"""A run outlives its terminal: leave it, close the window, open it again, add a benchmark, and end it cleanly."""
import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time

from typer.testing import CliRunner

from harbor_master import host, ledger, tasks
from harbor_master.cli import app

from conftest import make_benchmark


def shown(raw: bytes) -> str:
    plain = re.sub(rb"\x1b\[[0-9;?<>=]*[A-Za-z~]|\x1b[()][A-Z0-9]|\x1b[=>]|\x1b_[^\x1b]*\x1b\\\\", b"", raw)
    return re.sub(r"\s+", " ", plain.decode("utf8", "replace"))


def terminal(ws, args, until: str, then: bytes = b"", seconds: float = 30.0, close: bool = False) -> str:
    """Run `hm ARGS` in a terminal of its own until `until` is on it, type `then`, and return everything shown.
    With `close`, the terminal is closed on it instead, as when its window is."""
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.execv(sys.executable, [sys.executable, "-c", "from harbor_master.cli import app; app()", *args, "-w", str(ws)])
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 130, 0, 0))
    out, start, typed = b"", time.time(), False
    while time.time() - start < seconds:
        if select.select([fd], [], [], 0.1)[0]:
            try:
                got = os.read(fd, 65536)
            except OSError:
                got = b""
            if not got:
                break
            out += got
        if not typed and until in shown(out):
            typed = True
            if close:
                break
            for keys in ([then] if isinstance(then, bytes) else then):
                time.sleep(0.4)
                os.write(fd, keys)
        if os.waitpid(pid, os.WNOHANG)[0]:
            pid = 0
            while select.select([fd], [], [], 0.2)[0]:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    break
            break
    os.close(fd)
    if pid:
        time.sleep(0.3)
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
    return shown(out)


def wait(condition, seconds: float = 15.0) -> bool:
    for _ in range(int(seconds / 0.1)):
        if condition():
            return True
        time.sleep(0.1)
    return False


def test_a_run_outlives_its_terminal_and_takes_more_benchmarks(workspace, ident, tmp_path):
    run = ["curate", "demo-bench", "--simulate", "--speed", "0", "--budget", "100000"]
    listed = (workspace / ledger.CONTRIBUTORS).read_text()       # this is about the run and its terminal, not about verdicts:
    (workspace / ledger.CONTRIBUTORS).write_text(listed.replace(ident.github, "someone-else"))    # no report waits for this person
    try:
        first = terminal(workspace, run, "The run is finished", b"\x0c")           # it runs, and the user leaves with ctrl+l
        assert "working cells" in first and "demo-bench" in first and "The run goes on" in first
        going = host.active(workspace)
        assert going and going["simulate"] and "simulated run" in host.describe(going)

        again = CliRunner().invoke(app, ["curate", "--simulate", "--plain", "-w", str(workspace)])            # one run per index
        assert again.exit_code == 1 and "A run is going in this index already" in " ".join(again.output.split())

        closed = terminal(workspace, ["curate", "--simulate"], "working cells", close=True)    # opened again, then the window is closed
        assert "demo-bench" in closed and wait(lambda: host.active(workspace) is not None, 1) and host.active(workspace)["pid"] == going["pid"]

        tasks.register(workspace, "second", make_benchmark(tmp_path / "more", "second", 12), dev=True)
        assert host.tell(workspace, {"add": ["nothing"]})["ok"] is False
        added = terminal(workspace, ["curate", "second", "--simulate"], "second", b"\x0c")   # a benchmark is added to the run that is going
        assert "second is added to this run" in added and "The run goes on" in added
        assert "already in this run" in host.tell(workspace, {"add": ["second"]})["text"]
        real = CliRunner().invoke(app, ["curate", "second", "--plain", "-w", str(workspace)])
        assert real.exit_code == 1                                                # and a real run cannot start beside it

        ended = terminal(workspace, ["curate", "--simulate"], "second", (b"\x11", b"y"))    # ctrl+q, and yes: the run ends
        assert "The run has ended" in ended and wait(lambda: host.active(workspace) is None)
        assert not os.path.exists(going["socket"]) and not os.path.exists(going["control"])
        summaries = sorted((workspace / ".hm" / "runs").glob("*/run.json"))
        assert summaries and "second" in summaries[-1].read_text()                # one run, and it has both benchmarks

        terminal(workspace, run, "The run is finished", b"\x0c")                  # a new run, told to end from outside
        going = host.active(workspace)
        os.kill(going["pid"], signal.SIGTERM)
        assert wait(lambda: host.active(workspace) is None) and len(list((workspace / ".hm" / "runs").glob("*/run.json"))) == len(summaries) + 1
    finally:
        left = host.active(workspace)
        if left:
            os.kill(left["pid"], signal.SIGKILL)
