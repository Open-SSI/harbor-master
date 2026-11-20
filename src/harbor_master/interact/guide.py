"""Where an index stands on this machine, read without the network, and the one line of advice a run screen shows."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import ledger as ledger_mod
from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..core import identity as identity_mod
from ..core import keys as keys_mod


@dataclass
class State:
    """Where this machine stands."""
    ident: identity_mod.Identity | None = None
    ws: Path | None = None                              # the index being worked on
    title: str = ""
    owner: str = ""                                     # "Name (@handle)"
    me: dict | None = None                              # this person's entry on the contributor list
    can_curate: bool = False
    listed: list[str] = field(default_factory=list)     # benchmarks the index may curate
    to_adapt: list[str] = field(default_factory=list)   # the ones in another format that have no adapter yet
    abandoned: list[str] = field(default_factory=list)  # the ones given up, because no adapter could be made to pass
    lock: str = "none"                                  # none | locked | differs
    version: str = ""                                   # the locked protocol's version, such as 0.1.2
    released: bool = False                              # the owner released it: curation is closed, evaluation is open
    missing_keys: list[tuple[str, str]] = field(default_factory=list)   # (what for, variable)
    keys_file: Path | None = None
    registered: dict[str, dict] = field(default_factory=dict)
    running: dict | None = None                         # the run that is going in this index now, if one is
    ran: bool = False                                   # the ledger has records of a run
    ready: int = 0                                      # reports waiting for a verdict
    accepted: int = 0
    sim_ran: bool = False                               # the same three for simulated runs, whose records are
    sim_ready: int = 0                                  # kept on this machine and never count in the index
    sim_accepted: int = 0

    @property
    def real(self) -> list[str]:
        """The benchmarks a real run may take: the ones on the index's list. A run fetches them itself."""
        return sorted({*self.listed, *(b for b, info in self.registered.items() if info.get("supported"))} - set(self.abandoned))

    @property
    def trying(self) -> bool:
        """Nothing here can run for real, so the simulated runs are what there is to show."""
        return not self.real and not self.ran


def look(start: Path | None = None) -> State:
    s = State(ident=identity_mod.load(), ws=config_mod.find_workspace((start or Path.cwd()).resolve()))
    if s.ws is None:
        return s
    ws = s.ws
    try:
        cfg = config_mod.load(ws)
        entries = tasks_mod.supported.supported(ws)
        s.listed = [e["id"] for e in entries]
        from ..tasks import adapter
        s.to_adapt = [e["id"] for e in entries if e.get("format", "harbor") != "harbor"
                      and adapter.code_dir(adapter.folder(ws, e["id"])) is None]
    except (ValueError, OSError):       # a file of the index does not parse: the commands will say which
        cfg = config_mod.Config()
    people = ledger_mod.load_contributors(ws)
    s.title = cfg.about.title or cfg.about.name or ws.name
    s.owner = ", ".join(f"{c.get('name', '?')} (@{c['github']})" for c in people if c.get("role") == "owner" and c.get("github"))
    if s.ident is not None:
        s.me = next((c for c in people if c.get("github", "").lower() == s.ident.github.lower()), None)
        s.can_curate = ledger_mod.can_contribute(s.me)
    events = ledger_mod.Ledger(ws).events
    s.abandoned = sorted(ledger_mod.abandoned(events))
    s.to_adapt = [b for b in s.to_adapt if b not in s.abandoned]
    lock = ledger_mod.protocol.current(events)
    s.released = ledger_mod.evals.release(ledger_mod.evals.read(ws)) is not None
    if lock is not None:
        same = lock["protocol"] == cfg.protocol
        s.lock, s.version = "locked" if same else "differs", ledger_mod.protocol.version(lock)
    store = (cfg.about.tasks or "").strip()
    on_hub = bool(store) and not store.startswith(("/", "~", "."))       # a store on Hugging Face needs each person's own token
    s.missing_keys = [(what, var) for what, var, present, _ in keys_mod.doctor(cfg) if not present and (what != "task store" or on_hub)]
    s.keys_file = keys_mod.secrets_path()
    s.registered = tasks_mod.read_registry(ws)
    from .. import host
    s.running = host.active(ws)
    s.ran, s.ready, s.accepted = _counts(events)
    s.sim_ran, s.sim_ready, s.sim_accepted = _counts(ledger_mod.Ledger(config_mod.simulated(ws), simulated=True).events)
    return s


def _counts(events: list[dict]) -> tuple[bool, int, int]:
    states = ledger_mod.fold(events).values()
    return (any(e["type"] in ("decision", "trial") for e in events), sum(1 for t in states if t.status == "report_ready"),
            sum(1 for t in states if t.status == "accepted"))


def lock_line(s: State) -> str:
    """The index's lock, as a status: its owner locks it in a shell, and nobody does on a screen."""
    return {"none": "waiting for the owner's lock", "locked": f"{'released' if s.released else 'locked'} as v{s.version}",
            "differs": "this copy differs from the lock"}[s.lock]


def host_line(info: dict) -> str:
    import time
    return ("simulated" if info.get("simulate") else "real") + ", started " + time.strftime("%H:%M", time.localtime(info.get("started", 0)))


# ----- during a run -----

def session_tip(session) -> str:
    """One line for the run screen: what the run needs from you now, or what you can do while it works."""
    asks = sorted(session.requests, key=lambda r: (not r.blocking, r.kind == "verdict"))
    walk = getattr(session, "walk", None)
    if walk and not session.finished and not session.paused and not any(r.blocking for r in asks) and any(c.task for c in session.cells):
        name, told = walk[-1]       # a simulated run: what the stage it has just reached does
        return f"{name.capitalize()}. {told}"
    if asks:
        if asks[0].blocking:
            return ""           # the run asks in a box of its own, which says what is asked and what to type
        return "Reports are ready: the review page shows each one in turn. Read it, then type accept or reject <why> under it"
    if session.paused:
        return "The run is paused. ctrl+p goes on"
    if session.finished:
        return "The run is finished. ctrl+q closes it. What it did is on the index's screen, behind its buttons"
    if any(b.phase in ("fetch", "adapt") for b in session.benches.values()) and not any(c.task for c in session.cells):
        return "Getting the benchmark ready: fetching it, and building its adapter if it needs one. Nothing is needed from you"
    if any(b.phase in ("queued", "triage", "ranking") for b in session.benches.values()) and not any(c.task for c in session.cells):
        return "Screening the tasks. Nothing is needed from you yet"
    return "Nothing is needed from you. When something is, a box opens. Press a cell's number to see its task"
