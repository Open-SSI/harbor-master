"""What to do next: the steps from a new machine to a signed verdict, and a tip for the one you are at.

`look` reads where this machine stands, without the network. `steps` turns that into the
checklist, and `tip` into advice for the first step that is not done. `session_tip` is the
same idea during a run. The home screen, the run screen and `hm guide` all show these, so
the advice is written once.
"""
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
    folder: Path = field(default_factory=Path.cwd)      # where new index folders would go
    ws: Path | None = None                              # the index being worked on
    nearby: list[str] = field(default_factory=list)     # index folders right under `folder`, when not in one
    title: str = ""
    owner: str = ""                                     # "Name (@handle)"
    is_owner: bool = False
    me: dict | None = None                              # this person's entry on the contributor list
    can_curate: bool = False
    described: bool = False                             # about.toml has a description
    listed: list[str] = field(default_factory=list)     # benchmarks the index may curate
    lock: str = "none"                                  # none | locked | differs
    shared: str = ""                                    # the repository it is shared through
    skills_missing: int = 0                             # curation skills the index has no copy of
    missing_keys: list[tuple[str, str]] = field(default_factory=list)   # (what for, variable)
    keys_file: Path | None = None
    registered: dict[str, dict] = field(default_factory=dict)
    ran: bool = False                                   # the ledger has records of a run
    ready: int = 0                                      # reports waiting for a verdict
    accepted: int = 0
    sim_ran: bool = False                               # the same three for simulated runs, whose records are
    sim_ready: int = 0                                  # kept on this machine and never count in the index
    sim_accepted: int = 0

    @property
    def real(self) -> list[str]:
        """The registered benchmarks a real run may take: the ones on the index's list."""
        return [b for b, info in self.registered.items() if info.get("supported")]

    @property
    def trying(self) -> bool:
        """Nothing here can run for real, so the simulated runs are what there is to show."""
        return not self.real and not self.ran


def look(start: Path | None = None) -> State:
    folder = (start or Path.cwd()).resolve()
    s = State(ident=identity_mod.load(), folder=folder, ws=config_mod.find_workspace(folder))
    if s.ws is None:
        s.nearby = sorted(d.name for d in folder.iterdir() if d.is_dir() and (d / config_mod.PROTOCOL_NAME).exists()) \
            if folder.is_dir() else []
        return s
    ws = s.ws
    try:
        cfg = config_mod.load(ws)
        s.listed = [e["id"] for e in tasks_mod.supported.supported(ws)]
    except (ValueError, OSError):       # a file of the index does not parse: the commands will say which
        cfg = config_mod.Config()
    people = ledger_mod.load_contributors(ws)
    s.title, s.described = cfg.about.title or cfg.about.name or ws.name, bool(cfg.about.description)
    s.owner = ", ".join(f"{c.get('name', '?')} (@{c['github']})" for c in people if c.get("role") == "owner" and c.get("github"))
    if s.ident is not None:
        s.me = next((c for c in people if c.get("github", "").lower() == s.ident.github.lower()), None)
        s.is_owner = bool(s.me and s.me.get("role") == "owner" and s.me.get("key"))
        s.can_curate = ledger_mod.fields_of(people, s.ident.github) != set()
    events = ledger_mod.Ledger(ws).events
    lock = ledger_mod.protocol.current(events)
    if lock is not None:
        from .. import templates
        same = lock["protocol"] == cfg.protocol and all(templates.digests(ws).get(n) == d for n, d in (lock.get("registry") or {}).items())
        s.lock = "locked" if same else "differs"
    if (ws / ".git").exists():
        s.shared = ledger_mod.Sync(ws)._git("remote", "get-url", "origin", check=False).stdout.strip()
    from .. import skills
    s.skills_missing = len(skills.missing(ws / "skills"))
    s.missing_keys = [(what, var) for what, var, present, _ in keys_mod.doctor(cfg) if not present and what != "artifact store"]
    s.keys_file = keys_mod.secrets_path()
    s.registered = tasks_mod.read_registry(ws)
    s.ran, s.ready, s.accepted = _counts(events)
    s.sim_ran, s.sim_ready, s.sim_accepted = _counts(ledger_mod.Ledger(config_mod.simulated(ws), simulated=True).events)
    return s


def _counts(events: list[dict]) -> tuple[bool, int, int]:
    states = ledger_mod.fold(events).values()
    return (any(e["type"] in ("decision", "trial") for e in events), sum(1 for t in states if t.status == "report_ready"),
            sum(1 for t in states if t.status == "accepted"))


# ----- the checklist -----

STEPS = ("Who you are", "An index", "Your place in it", "The protocol", "Keys", "A benchmark", "A run", "Your verdicts")


def steps(s: State) -> list[tuple[str, str, str]]:
    """Every step as (name, status, detail). Status is done, next, later, or optional."""
    rows: list[tuple[str, bool, str]] = [("Who you are", s.ident is not None, s.ident.label() if s.ident else "not set up yet")]
    if s.ws is None:
        rows.append(("An index", False, f"none here ({len(s.nearby)} in this folder)" if s.nearby else "none here yet"))
        rows += [(name, False, "") for name in STEPS[2:]]
    else:
        place = ("you own it" if s.is_owner else "you can curate" if s.can_curate else
                 "listed, but you cannot curate yet" if s.me else "you are not on its contributor list")
        keys = "all found" if not s.missing_keys else f"{len(s.missing_keys)} missing, needed for real runs"
        rows += [
            ("An index", True, f"{s.title}   {s.ws}"),
            ("Your place in it", s.can_curate, place + (f"   owner: {s.owner}" if s.owner and not s.is_owner else "")),
            ("The protocol", s.lock == "locked", {"none": "not locked yet, needed for real runs", "locked": "locked",
                                                  "differs": "this copy is not the locked one"}[s.lock]),
            ("Keys", not s.missing_keys, keys),
            ("A benchmark", bool(s.registered), ", ".join(s.registered) or "none registered here"),
            ("A run", s.ran or (s.trying and s.sim_ran), "the ledger has records" if s.ran else
             "simulated only: kept on this machine, not in the index" if s.sim_ran else "nothing has run yet"),
            ("Your verdicts", bool(s.accepted) and not s.ready, f"{s.ready} report{'s' if s.ready != 1 else ''} waiting, {s.accepted} accepted")
            if not (s.trying and s.sim_ran) else
            ("Your verdicts", bool(s.sim_accepted) and not s.sim_ready,
             f"simulated: {s.sim_ready} report{'s' if s.sim_ready != 1 else ''} waiting, {s.sim_accepted} accepted"),
        ]
    out, found_next = [], False
    for name, done, detail in rows:
        status = "done" if done else "later"
        if not done and not found_next and name == next_step(s):
            status, found_next = "next", True
        out.append((name, status, detail))
    return out


def next_step(s: State) -> str:
    """The step the tip is about. A simulated run needs no lock and no keys, so those two do not stand in
    the way of a benchmark that only runs simulated."""
    if s.ident is None:
        return "Who you are"
    if s.ws is None:
        return "An index"
    if s.ready:
        return "Your verdicts"
    if not s.registered:
        return "Your place in it" if not s.can_curate and not s.is_owner and s.listed else "A benchmark"
    if s.real:      # a benchmark that can run for real: clear what a real run needs, in order
        if not s.can_curate:
            return "Your place in it"
        if s.lock != "locked":
            return "The protocol"
        if s.missing_keys:
            return "Keys"
    if s.trying:
        return "Your verdicts" if s.sim_ready or s.sim_accepted else "A run"
    return "Your verdicts" if s.ran and s.accepted else "A run"


@dataclass
class Tip:
    title: str
    lines: list[tuple[str, str]]        # (command or "", what it does or a sentence)


TRY = ("/demo", "Or try it all on made-up tasks first. It spends nothing")
APART = ("", "A simulated run keeps its records on this machine, marked as simulated. They are never in the index, "
             "and never count as evidence.")


def _meanwhile(s: State, without: str) -> tuple[str, str]:
    """What can be done before a real run is possible."""
    return ("/run --simulate", f"Meanwhile, a simulated run needs no {without}, and leaves the index untouched")


def tip(s: State) -> Tip:
    """Advice for the step `next_step` names: what it is for, and the commands that do it."""
    step = next_step(s)
    if step == "Who you are":
        return Tip("Start here: say who you are", [
            ("/init", "Asks your name, affiliation, email and GitHub handle, and makes your signing key"),
            ("", "Every record you make is signed with that key, and credited to your handle. Nothing is sent anywhere."),
            ("/help", "Lists every command. Press ctrl+q to leave at any time")])
    if step == "An index":
        lines = [("/indexes", "See the indexes that are listed"),
                 ("/join NAME", "Join one, by its name on that list or by its address. It is cloned into a folder here"),
                 ("/new NAME", "Start an index of your own. You become its owner")]
        if s.nearby:
            lines.insert(0, ("/use FOLDER", "Open one that is already in this folder: " + ", ".join(s.nearby)))
        return Tip("Next: pick an index to work on", lines + [TRY])
    if step == "Your place in it":
        if s.me is None or not s.me.get("key"):
            return Tip("Next: ask the owner to add you", [
                ("", f"You are not on this index's contributor list, so it accepts no records from you yet. Its owner is {s.owner or 'not listed'}."),
                ("/key", "Shows your public key. Send it to the owner, who adds you and locks the list"),
                ("/sync", "Brings in the owner's change once they have made it"),
                TRY])
        return Tip("Next: ask the owner for your fields", [
            ("", f"You are on the list, but with no fields set, so you cannot curate yet. Ask {s.owner or 'the owner'} to set them."),
            ("/contributors", "Shows the list and what each person may curate"),
            ("/sync", "Brings in the owner's change once they have made it"),
            TRY])
    if step == "The protocol":
        if s.is_owner:
            lines = []
            if not s.described:
                lines.append(("", "Describe the index in about.toml, so that others know what it is."))
            if not s.listed:
                lines.append(("", "List the benchmarks it may curate in benchmarks.toml. The file explains each field."))
            if s.skills_missing:
                lines.append(("/skills adopt", f"Copies the {s.skills_missing} skills this index lacks into it. What agents are told is then "
                                               "the index's own, and is locked with it"))
            else:
                lines.append(("/skills", "What agents are told in this index. Edit a file under skills/ to word it your way"))
            lines.append(("/protocol", "Shows the settings a run would use, and what differs from the locked ones"))
            lines.append(("/lock", "Signs the settings, the benchmark list, the roster and the contributor list. Real runs need it"))
            if not s.shared:
                lines.append(("/connect URL", "Shares the index through a git repository, so that others can join it"))
            lines.append(_meanwhile(s, "lock"))
            return Tip("Next: lock the protocol" if s.lock == "none" else "Next: lock the changed protocol", lines)
        why = "has not locked a protocol yet" if s.lock == "none" else "locked other settings than this copy has"
        return Tip("Next: wait for the owner's lock", [
            ("", f"A real run needs one fixed set of settings, and {s.owner or 'the owner'} {why}."),
            ("/protocol", "Shows what differs"),
            ("/sync", "Brings in the owner's lock, and their copy of the files"),
            _meanwhile(s, "lock")])
    if step == "Keys":
        need = "; ".join(f"{what}: {var}" for what, var in s.missing_keys[:4])
        have = s.keys_file is not None and s.keys_file.exists()
        return Tip("Next: add the keys a real run needs", [
            ("", f"Missing: {need}."),
            ("/keys", f"Fill them in at {s.keys_file}" if have else f"Creates the keys file at {s.keys_file}, for you to fill in"),
            ("/doctor", "Shows which keys are found, and what this machine can run"),
            _meanwhile(s, "keys")])
    if step == "A benchmark":
        lines = [("/benchmarks", "Shows the benchmarks this index may curate" + ("" if s.listed else ". None is listed yet"))]
        if s.listed:
            lines.append((f"/submit {s.listed[0]}", "Fetches one at its pinned release and registers it here"))
        elif s.is_owner:
            lines.append(("", "To curate a real benchmark, list it in benchmarks.toml and lock the change with /lock."))
        lines.append(("/demo", "Adds a made-up benchmark, for simulated runs only. It spends nothing"))
        return Tip("Next: get a benchmark to curate", lines)
    if step == "A run":
        lines = []
        if s.real and s.can_curate and s.lock == "locked" and not s.missing_keys:
            lines.append(("/run", "Runs for real: models decide, agents attempt the tasks, and it costs money"))
        elif s.real:
            lines.append(("", "A real run needs your place on the list, a locked protocol, and keys: see the steps above."))
        lines.append(("/run --simulate", "The same screen on simulated models and trials. It spends nothing"))
        lines.append(("", "The run stops twice to ask you something: first for a brief on what matters in the benchmark, "
                          "then to approve its selection. A tip on that screen says how to answer."))
        if s.sim_ran:
            lines.append(("/status --simulated", "Where your simulated runs stand"))
        lines.append(APART)
        return Tip("Next: run it", lines)
    if s.ready:
        return Tip(f"Next: {s.ready} report{'s are' if s.ready != 1 else ' is'} waiting for your verdict", [
            ("/run", "Opens the run again. Nothing is run twice: it goes straight to the reports"),
            ("", "There, /review lists the reports, /report ID shows one, and /accept ID or /reject ID <reason> signs your verdict."),
            ("/status", "Where every task stands")])
    if s.trying and s.sim_ready:
        return Tip(f"Next: {s.sim_ready} simulated report{'s are' if s.sim_ready != 1 else ' is'} waiting for your verdict", [
            ("/run --simulate", "Opens the run again. Nothing is run twice: it goes straight to the reports"),
            ("", "There, /review lists the reports, /report ID shows one, and /accept ID or /reject ID <reason> signs your verdict."),
            ("/status --simulated", "Where every simulated task stands"),
            APART])
    if s.trying:
        return Tip("You have been through it all, on simulated tasks", [
            ("/leaderboard --simulated", "How each agent configuration did on the tasks you accepted"),
            ("/status --simulated", "Where every simulated task stands"),
            ("/benchmarks", "For real work: the benchmarks this index may curate, then /submit ID"),
            APART])
    return Tip("The index has accepted tasks", [
        ("/leaderboard", "How each agent configuration does on the accepted tasks"),
        ("/todo", "What is still to benchmark: each roster configuration on each accepted task"),
        ("/status", "Where every task stands"),
        ("/run", "Curate more: tasks that are done are not run again")])


def shell(command: str) -> str:
    """A home screen command as it is typed in a shell: `/run --simulate` is `hm run --simulate`."""
    if not command:
        return ""
    head, _, rest = command.partition(" ")
    return (IN_A_SHELL.get(head) or "hm " + head[1:]) + (" " + rest if rest else "")


# Where a home screen command is not simply `hm NAME` in a shell.
IN_A_SHELL = {"/new": "hm index new", "/lock": "hm protocol lock", "/connect": "hm index connect", "/keys": "hm keys init",
              "/use": "cd", "/verify": "hm ledger verify", "/entry": "hm index entry", "/help": "hm --help"}


def text(s: State) -> str:
    """The checklist and the tip as plain lines, for `hm guide` and where there is no terminal."""
    mark = {"done": "[x]", "next": "[>]", "later": "[ ]"}
    out = [f"{mark[status]} {n}. {name}" + (f": {detail}" if detail else "") for n, (name, status, detail) in enumerate(steps(s), 1)]
    advice = tip(s)
    out += ["", advice.title]
    out += [f"  {shell(command):<22} {what}" if command else f"  {what}" for command, what in advice.lines]
    return "\n".join(out)


# ----- during a run -----

def session_tip(session) -> str:
    """One line for the run screen: what the run needs from you now, or what you can do while it works."""
    asks = sorted(session.requests, key=lambda r: (not r.blocking, r.kind == "verdict"))
    if asks:
        r = asks[0]
        short = r.target.split("/")[-1]
        if r.kind == "brief":
            return f"The run waits for you. Say what matters in {r.target}: /brief {r.target} <one or two sentences>"
        if r.kind == "selection":
            return f"The run waits for you. /selection {short} shows the ranking, /approve {short} starts the costly stages"
        if r.kind == "budget":
            return "The budget is spent and the run is paused. Raise it with /budget N, or leave with /quit"
        if r.kind == "replace":
            return f"The benchmark is full. /included {r.target.rsplit('/', 1)[0] if '/' in r.target else ''} compares its tasks, /replace OLD {short} swaps one"
        return "Reports are ready. /review lists them, /report ID shows one, /accept ID or /reject ID <reason> signs your verdict"
    if session.paused:
        return "The run is paused. /resume continues it"
    if session.finished:
        return "The run is finished. ctrl+q leaves, and what it did is in: hm status"
    if any(b.phase in ("queued", "triage", "ranking", "adapt") for b in session.benches.values()) and not any(c.task for c in session.cells):
        return "Screening the tasks. Nothing is needed from you yet. Ask anything in plain words, such as: what happens next?"
    return "Nothing is needed from you. Press 1-9 to open a cell, ask a question in plain words, or type / for commands"
