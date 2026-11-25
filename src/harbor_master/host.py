"""A run that outlives the terminal it was started from (DESIGN.md Section 10).

The run and its screen live in a background process, attached to a pseudo-terminal that a
small relay owns. What a person has in their terminal is a view of that screen: closing the
window closes the view, and `hm curate` opens another on the same run. There is one run per
index. `/add NAME` on its screen adds a benchmark to the one that is going.

    terminal  <-->  view (hm curate)  <-- socket -->  relay  <-- pseudo-terminal -->  the run and its screen
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import contextlib
import os
import pty
import select
import selectors
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty
from pathlib import Path

from .core import config as config_mod

ACTIVE = "run.active"           # in .hm: which run is going in this index
DETACH = b"\x1b_hm-detach\x1b\\"        # written by the screen when the user leaves it: the relay closes every view
# What a view sets up in the terminal it runs in, and puts back: the other screen, no cursor, pasted text. Not the
# mouse: the screen switches it on itself once it is drawn, and the relay repeats that to a view that comes later.
# Switched on any earlier, every mouse movement before the screen starts would be echoed back as text.
ENTER = b"\x1b[?1049h\x1b[H\x1b[2J\x1b[?25l\x1b[?2004h"
MOUSE_ON, MOUSE_OFF = b"\x1b[?1000h\x1b[?1003h\x1b[?1015h\x1b[?1006h", b"\x1b[?1000l"
# Everything the screen may have switched on in the terminal is switched off again on the way out. The screen
# itself is still running behind the relay, so it never does this for a view that leaves. The keyboard
# protocol matters most: left on, every key typed in the shell afterwards shows up as codes such as 4;;104u.
LEAVE = (b"\x1b[=0;1u\x1b[<u"                                                        # the extended keyboard protocol
         b"\x1b[?2004l\x1b[?1004l\x1b[?2048l\x1b[?2026l"                             # paste, focus, resize and sync reports
         b"\x1b[?1016l\x1b[?1006l\x1b[?1015l\x1b[?1003l\x1b[?1002l\x1b[?1000l"       # the mouse
         b"\x1b[0m\x1b[?25h\x1b[?1049l")                                             # colors, the cursor, the screen
OTHER, BACK = b"\x1b[?1049h", b"\x1b[?1049l"
# The most of the screen's output the relay holds for one view that is not taking it (a slow link, a terminal whose
# window is asleep). The relay never waits for a view: if it did, the screen behind it could not write, and the whole
# run would stop with it. Nor can it drop part of what it holds, since each piece of output only makes sense on top of
# what came before. So a view that falls this far behind loses all it holds, is sent nothing more, and once it takes
# output again it is sent the whole screen afresh. It is never closed for being slow.
MAX_BACKLOG = 256 * 1024
# Sent to a view that caught up, before the screen is drawn again: ends any control sequence that was cut off when its
# backlog was thrown away (CAN), resets the colors, and clears what was left on it.
RESYNC = b"\x18\x1b[0m\x1b[H\x1b[2J"


def _note(what: str) -> None:
    """A line in host.log, where the relay's output goes."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {what}", flush=True)


def back_already(back: bool, tail: bytes, shown: bytes) -> tuple[bool, bytes]:
    """Whether the run's own screen has put the terminal back on its first screen, given what was known before (`back`,
    and the last few bytes seen, in case a mark is split between two reads) and what was just shown. A screen that ends
    does, and then prints its summary there: leaving the other screen a second time would move the cursor back up over
    that summary, so the view must not. Returns the new `back`, and the bytes to keep for next time."""
    seen = tail + shown
    on, off = seen.rfind(OTHER), seen.rfind(BACK)
    if on != off:           # both -1 when neither is in it: nothing changed
        back = off > on
    return back, seen[-(len(BACK) - 1):]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sockets(ws: Path) -> tuple[str, str]:
    """Where the relay and the run listen. A socket's path must be short, so they are not in the index's folder."""
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / f"hm-{os.getuid()}"
    base.mkdir(mode=0o700, exist_ok=True)
    name = hashlib.sha256(str(ws.resolve()).encode()).hexdigest()[:16]
    return str(base / f"{name}.view"), str(base / f"{name}.ctl")


def active(ws: Path) -> dict | None:
    """The run that is going in this index, or None. A record left by a run that died is cleared."""
    path = config_mod.local(ws) / ACTIVE
    try:
        info = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if _alive(int(info.get("pid", 0) or 0)):
        return info
    for leftover in (path, Path(info.get("socket") or "/nonexistent"), Path(info.get("control") or "/nonexistent")):
        leftover.unlink(missing_ok=True)
    return None


def claim(ws: Path, command: str, simulate: bool, pid: int | None = None, socket_path: str = "", control: str = "", app: int = 0) -> Path:
    """Write down that a run is going here. Returns the file, to remove when the run ends."""
    path = config_mod.local(ws) / ACTIVE
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"pid": pid or os.getpid(), "app": app, "started": time.time(), "command": command,
                                "simulate": simulate, "socket": socket_path, "control": control}) + "\n")
    return path


def describe(info: dict) -> str:
    kind = "a simulated run" if info.get("simulate") else "a real run"
    return f"{kind}, started {time.strftime('%Y-%m-%d %H:%M', time.localtime(info.get('started', 0)))} ({info.get('command', 'hm run')})"


# ----- the relay: the background process that owns the run's terminal -----

def _resize(master: int, rows: int, cols: int) -> None:
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 5), max(cols, 20), 0, 0))


def relay(ws: Path, argv: list[str], command: str, simulate: bool, verb: str = "curate") -> None:
    """Start the run on a pseudo-terminal and carry its screen to whoever is viewing. Returns when the run ends."""
    view_path, control_path = _sockets(ws)
    for stale in (view_path, control_path):
        Path(stale).unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(view_path)
    os.chmod(view_path, 0o600)
    server.listen(4)
    child, master = pty.fork()
    if child == 0:      # the run itself: the same command, told that it is the one behind the relay
        os.environ.update(HM_HOSTED="1", HM_CONTROL=control_path)
        signal.signal(signal.SIGTSTP, signal.SIG_IGN)       # ctrl+z quits the run: it never pauses it, whatever the
                                                            # terminal is doing at the moment it is pressed
        os.environ.setdefault("TERM", "xterm-256color")
        os.execv(sys.executable, [sys.executable, "-c", "from harbor_master.cli import app; app()", verb, *argv,
                                  "--foreground", "-w", str(ws)])
    _resize(master, 40, 120)
    record = claim(ws, command, simulate, socket_path=view_path, control=control_path, app=child)
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))        # told to end: ask the run to close its books
    sel = selectors.DefaultSelector()
    sel.register(server, selectors.EVENT_READ, "server")
    sel.register(master, selectors.EVENT_READ, "run")
    views: dict[socket.socket, bytearray] = {}      # what each view has sent that is not read yet
    unsent: dict[socket.socket, bytearray] = {}     # the screen's output each view has not taken yet
    tail, later = b"", None         # the end of the last output, in case the mark is split; and a size to set in a moment
    size = (40, 120)                # the screen's size as the views last gave it
    behind: set[socket.socket] = set()      # views whose backlog was thrown away: sent nothing until they take output
    mouse = False                   # whether the screen has switched the mouse on, for a view that opens later

    def drop(view: socket.socket) -> None:
        if view not in views:
            return
        sel.unregister(view)
        views.pop(view, None)
        unsent.pop(view, None)
        behind.discard(view)
        view.close()

    def redraw() -> None:
        """Have the screen draw itself again in full: a size it does not have, then its own size back."""
        nonlocal later
        if later is None:
            _resize(master, size[0], size[1] - 1)
            later = (time.time() + 0.08, *size)

    def send(view: socket.socket, data: bytes) -> None:
        """Give a view the screen's output without ever waiting for it: what it does not take now waits in its backlog,
        sent as it reads. One whose backlog passes MAX_BACKLOG loses it, and is drawn afresh once it reads again."""
        if view not in views or view in behind:
            return
        box = unsent[view]
        if not box:
            try:
                n = view.send(data)
            except (BlockingIOError, InterruptedError):
                n = 0
            except OSError:
                drop(view)
                return
            data = data[n:]
        box += data
        if len(box) > MAX_BACKLOG:
            box.clear()
            behind.add(view)
            _note(f"a view fell {MAX_BACKLOG // 1024} KB behind: what it had is dropped, and it is drawn afresh when it reads")
            sel.modify(view, selectors.EVENT_READ | selectors.EVENT_WRITE, "view")
            return
        sel.modify(view, selectors.EVENT_READ | (selectors.EVENT_WRITE if box else 0), "view")

    def flush(view: socket.socket) -> None:
        box = unsent[view]
        if view in behind:          # it takes output again: the whole screen, from a clean start
            behind.discard(view)
            box += RESYNC
            redraw()
            _note("the view is reading again: the screen is drawn afresh")
        try:
            n = view.send(box)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            drop(view)
            return
        del box[:n]
        if not box:
            sel.modify(view, selectors.EVENT_READ, "view")

    try:
        while True:
            if stopping:
                stopping.clear()
                os.kill(child, signal.SIGTERM)
            if later and time.time() >= later[0]:
                _resize(master, later[1], later[2])
                later = None
            for key, mask in sel.select(0.05):
                if key.data == "server":
                    view, _ = server.accept()
                    view.setblocking(False)
                    views[view], unsent[view] = bytearray(), bytearray()
                    sel.register(view, selectors.EVENT_READ, "view")
                    if mouse:               # the screen is up: this view's terminal reports the mouse too
                        send(view, MOUSE_ON)
                elif key.data == "run":
                    try:
                        data = os.read(master, 65536)
                    except OSError:         # the run has ended and closed its terminal
                        data = b""
                    if not data:
                        raise EOFError
                    data, left = tail + data, False
                    if b"\x1b[?1000h" in data or b"\x1b[?1003h" in data:
                        mouse = True
                    elif MOUSE_OFF in data:
                        mouse = False
                    if DETACH in data:
                        data, left = data.replace(DETACH, b""), True
                    keep = next((n for n in range(min(len(DETACH) - 1, len(data)), 0, -1) if DETACH.startswith(data[-n:])), 0)
                    data, tail = (data[:-keep], data[-keep:]) if keep else (data, b"")
                    for view in list(views):
                        send(view, data)
                    if left:                # the user left the screen: every view closes, and the run goes on
                        for view in list(views):
                            drop(view)
                else:
                    view = key.fileobj
                    if view not in views:   # let go earlier in this same pass
                        continue
                    if mask & selectors.EVENT_WRITE:
                        flush(view)
                        if view not in views or not mask & selectors.EVENT_READ:
                            continue
                    try:
                        got = view.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        got = b""
                    if not got:
                        drop(view)
                        _note("a view closed")
                        if not views:       # the last terminal closed without leaving: the run decides if it goes on
                            with contextlib.suppress(ProcessLookupError):
                                os.kill(child, signal.SIGUSR1)
                        continue
                    buf = views[view]
                    buf += got
                    while len(buf) >= 5:    # one letter and a length, then that many bytes
                        kind, length = chr(buf[0]), int.from_bytes(buf[1:5], "big")
                        if len(buf) < 5 + length:
                            break
                        body = bytes(buf[5:5 + length])
                        del buf[:5 + length]
                        if kind == "I":
                            os.write(master, body)
                        elif kind == "R" and length == 4:
                            size = struct.unpack(">HH", body)
                            later = None
                            redraw()            # in full, for a view that just opened or a terminal that changed size
    except EOFError:
        pass
    finally:
        record.unlink(missing_ok=True)      # first, so that a closing view knows the run has ended
        for view in list(views):
            view.close()
        server.close()
        for leftover in (view_path, control_path):
            Path(leftover).unlink(missing_ok=True)
        try:
            os.waitpid(child, 0)
        except ChildProcessError:
            pass


def start(ws: Path, argv: list[str], command: str, simulate: bool, verb: str = "curate") -> dict:
    """Start a run in the background, behind a relay, and wait until it can be viewed."""
    log = config_mod.local(ws) / "host.log"
    log.parent.mkdir(exist_ok=True)
    with log.open("ab") as out:
        subprocess.Popen([sys.executable, "-m", "harbor_master.host", str(ws), json.dumps(argv), command, "1" if simulate else "0", verb],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, cwd=str(ws))
    for _ in range(200):
        info = active(ws)
        if info and info.get("socket") and Path(info["socket"]).exists():
            return info
        time.sleep(0.05)
    raise RuntimeError(f"The run did not start. See {log}")


# ----- the view: what `hm curate` shows when a run is going -----

def _frame(kind: str, body: bytes) -> bytes:
    return kind.encode() + len(body).to_bytes(4, "big") + body


def _size() -> bytes:
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        rows, cols = 40, 120
    return struct.pack(">HH", rows or 40, cols or 120)


def _write_all(fd: int, data: bytes) -> None:
    """Write all of it to the terminal. One write can take only part: a signal (the terminal changing size, as it does
    when its pane is shown again) cuts short a write to a terminal that reads slowly, and the rest would be lost: a
    whole screen drawn afresh then shows only what changes after it."""
    view = memoryview(data)
    while view:
        try:
            view = view[os.write(fd, view):]
        except BlockingIOError:             # a terminal left non-blocking: wait until it takes more
            select.select([], [fd], [], 1)


def attach(ws: Path) -> str:
    """Show the run that is going in this terminal until the user leaves it or it ends.
    Returns "going" when the run is still there afterwards, and "ended" when it is not."""
    info = active(ws)
    if info is None or not info.get("socket"):
        return "ended"
    link = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        link.connect(info["socket"])
    except OSError:
        return "ended"
    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
    before = termios.tcgetattr(stdin)
    resized = [True]
    previous = signal.signal(signal.SIGWINCH, lambda *_: resized.append(True))
    sel = selectors.DefaultSelector()
    sel.register(stdin, selectors.EVENT_READ, "keys")
    sel.register(link, selectors.EVENT_READ, "screen")
    back, tail = False, b""
    try:
        tty.setraw(stdin)
        os.write(stdout, ENTER)
        while True:
            if resized:
                resized.clear()
                link.sendall(_frame("R", _size()))
            for key, _ in sel.select(0.2):
                if key.data == "keys":
                    typed = os.read(stdin, 4096)
                    if not typed:
                        return "going"      # the terminal is gone
                    link.sendall(_frame("I", typed))
                else:
                    shown = link.recv(65536)
                    if not shown:
                        return "going" if active(ws) else "ended"
                    back, tail = back_already(back, tail, shown)
                    _write_all(stdout, shown)
    except OSError:
        return "going" if active(ws) else "ended"
    finally:
        signal.signal(signal.SIGWINCH, previous)
        try:
            os.write(stdout, LEAVE.removesuffix(BACK) if back else LEAVE)
            time.sleep(0.05)                               # what the terminal still had to say arrives
            termios.tcflush(stdin, termios.TCIFLUSH)        # and is dropped, so that it is not typed into the shell
        except (OSError, termios.error):
            pass
        termios.tcsetattr(stdin, termios.TCSADRAIN, before)
        link.close()


def tell(ws: Path, request: dict, timeout: float = 30.0) -> dict:
    """Ask the run that is going to do something, such as take one more benchmark. Returns its reply."""
    info = active(ws)
    if info is None or not info.get("control"):
        return {"ok": False, "text": "No run with a screen is going in this index."}
    link = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    link.settimeout(timeout)
    try:
        for _ in range(100):            # the run opens this door a moment after it starts
            try:
                link.connect(info["control"])
                break
            except OSError:
                time.sleep(0.1)
        else:
            return {"ok": False, "text": "The run that is going does not answer."}
        link.sendall(json.dumps(request).encode() + b"\n")
        reply = b""
        while not reply.endswith(b"\n"):
            got = link.recv(65536)
            if not got:
                break
            reply += got
        return json.loads(reply or b'{"ok": false, "text": "The run gave no answer."}')
    except (OSError, ValueError) as err:
        return {"ok": False, "text": f"Could not reach the run that is going: {err}"}
    finally:
        link.close()


if __name__ == "__main__":      # the relay, started by `start`
    relay(Path(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3], sys.argv[4] == "1", *sys.argv[5:6])
