"""Setting up: identity, an index to work on, keys."""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from string import Template
from typing import Optional

import tomli_w
import typer
from rich.table import Table

from .. import ledger as ledger_mod
from .. import skills
from ..core import config as config_mod
from ..core import identity as identity_mod
from .. import templates
from ..core import keys
from ..tui import theme
from .listing import read_list
from .common import Workspace, app, check_owner, console, fail, is_owner, need_identity, need_workspace, set_up_local


@app.command()
def init(name: str = typer.Option(None), affiliation: str = typer.Option(None), email: str = typer.Option(None),
         github: str = typer.Option(None, help="GitHub handle. It identifies you in the ledger.")):
    """Everyone starts here. It asks who you are and creates your signing key, then goes on to an index, your keys, and a
    simulated demo of curating or of evaluating. One box at a time. Run it again to change your details: each box shows
    what this machine has, and your signing key is kept."""
    import sys
    if not any((name, affiliation, email, github)) and sys.stdout.isatty() and sys.stdin.isatty():
        from .walk import init_walk
        init_walk()
        return
    old = identity_mod.load()
    ask = lambda label, given, default: given or typer.prompt(label, default=default or None)  # noqa: E731
    ident = identity_mod.create(
        ask("Name", name, old and old.name), ask("Affiliation", affiliation, old and old.affiliation),
        ask("Email", email, old and old.email), ask("GitHub handle", github, old and old.github))
    said(ident)


def said(ident: identity_mod.Identity) -> None:
    """What hm init leaves printed: who you are, and the line an index's owner needs."""
    console.print(f"You are [bold]{ident.label()}[/].", highlight=False)
    console.print(f"[{theme.MU}]To join an index, send its owner this line. They paste it in a shell, inside the index's folder:[/]")
    console.print(ident.line(), markup=False, highlight=False, soft_wrap=True)
    console.print(f"[{theme.MU}]Next: hm indexes lists the indexes, and hm open INDEX clones one.[/]")


def _skills_line(ws: Path) -> str:
    states = [state for _, state in skills.status(ws / "skills")]
    own, absent = states.count("own"), states.count("missing")
    if absent:
        return f"{absent} of {len(states)} not in the index yet: hm lock copies them in"
    return f"{len(states)} in the index, " + (f"{own} in its own wording" if own else "all as the templates have them")


def _describe(ws: Path) -> None:
    """What an index is: the lines `hm index` and `hm open INDEX` both print."""
    cfg = config_mod.load(ws)
    led = ledger_mod.Ledger(ws)
    people = ledger_mod.load_contributors(ws)
    owners = [f"{c.get('name', '?')} (@{c['github']})" for c in people if c.get("role") == "owner" and c.get("github")]
    lock = ledger_mod.protocol.current(led.events)
    sync = ledger_mod.Sync(ws)
    remote = sync._git("remote", "get-url", "origin", check=False).stdout.strip() if (ws / ".git").exists() else ""
    console.print(f"[bold]{cfg.about.title or cfg.about.name or ws.name}[/]" + (f"  [{theme.MU}]{cfg.about.name}[/]" if cfg.about.title else ""))
    if cfg.about.description:
        console.print(cfg.about.description)
    from .. import tasks as tasks_mod
    rows = [("owner", ", ".join(owners) or "none listed"),
            ("contributors", f"{len(ledger_mod.active(people))} can contribute, {len(people)} listed"),
            ("benchmarks", ", ".join(e["id"] for e in tasks_mod.supported.supported(ws)) or "none listed yet"),
            ("accepted tasks", str(len(ledger_mod.index_entries(led.events)))),
            ("protocol", f"{cfg.protocol}, " + (f"locked as v{ledger_mod.protocol.version(lock)}" if lock and lock["protocol"] == cfg.protocol else
                                                 "not the locked one" if lock else "not locked yet")),
            ("skills", _skills_line(ws)),
            ("shared at", remote or "nowhere yet"),
            ("folder", str(ws))]
    for label, value in rows:
        console.print(f"  [{theme.MU}]{label:<15}[/]{value}")


def index_new(name: str, directory: Path | None = None, title: str = "", description: str = "", tasks: str = "", url: str = "",
              trial: bool = False) -> Path:
    """Start a new index with you as its owner: the files that define it, in a git repository of its own. What hm new does,
    and the practice index of hm init, which is `trial`: for made-up tasks only, so it keeps none."""
    ident = need_identity()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        fail("An index's name is lower case letters, digits and hyphens, such as openssi.")
    from ..tasks import store
    tasks = tasks.strip()
    if not trial and not store.is_name(tasks):
        fail("An index needs a store for its accepted tasks, so that everyone can run exactly what was accepted: "
             "--tasks ORG/NAME, a Hugging Face dataset repository. It is created for you if it does not exist.")
    ws = (directory or Path(name)).resolve()
    if ws.exists() and any(ws.iterdir()):
        fail(f"{ws} is not empty.")
    ws.mkdir(parents=True, exist_ok=True)
    import uuid
    fill = lambda text: Template(text).substitute(name=name, title=title or name, canary=uuid.uuid4())  # noqa: E731
    for file in ("protocol.toml", "roster.toml"):
        (ws / file).write_text(templates.read(file))
    about = fill(templates.read("about.toml"))
    for key, value in (("description", description), ("tasks", tasks)):
        about = about.replace(f'{key} = ""', f"{key} = {json.dumps(value.strip(), ensure_ascii=False)}")
    (ws / config_mod.ABOUT_NAME).write_text(about)
    (ws / "README.md").write_text(fill(templates.read("index-readme.md")))
    (ws / ".gitignore").write_text(templates.read("gitignore"))
    skills.adopt(ws / "skills", skills.names())         # what agents are told is the index's own, from the first commit
    (ws / ledger_mod.CONTRIBUTORS).write_text(templates.read("contributors.toml") + tomli_w.dumps({"contributor": [{
        "name": ident.name, "affiliation": ident.affiliation, "email": ident.email, "github": ident.github,
        "key": ident.public_key, "role": "owner", "added": time.strftime("%Y-%m-%d")}]}))
    ledger_mod.sign_list(ws, ident, config_mod.load(ws).protocol, "start the index")
    try:
        ledger_mod.Sync(ws, ident.name, ident.github).define(f"start the {name} index")
        ledger_mod.Sync(ws, ident.name, ident.github)._commit("sign the contributor list")
    except ledger_mod.SyncError as err:
        fail(str(err))
    set_up_local(ws)
    check_owner(ws)
    console.print(f"Started the [bold]{title or name}[/] index at {ws}, with you as its owner.")
    if not trial:
        from .store import make_store
        make_store(config_mod.load(ws))
    if url:
        try:
            state = ledger_mod.Sync(ws, ident.name, ident.github).connect(url)
            console.print(f"[{theme.TE}]It {state}[/] at {url}. Others get it with: hm open {url}", highlight=False)
        except ledger_mod.SyncError as err:
            console.print(f"[{theme.AM}]It is not shared yet:[/] {err}\nThe index itself is fine. Once the repository exists and is "
                          f"empty, inside the index's folder: git remote add origin {url}, then hm sync", highlight=False)
    console.print(f"[{theme.MU}]Next: cd {ws.name}. Edit about.toml, protocol.toml, roster.toml and skills/ as you want them, then "
                  "hm lock. hm contributors add lets someone in, hm curate builds the index, and hm open shows where it stands.[/]", highlight=False)
    return ws


def connect(ws: Path, url: str) -> None:
    """Share an index through a git repository, so that others can join it and see each other's claims."""
    ident = need_identity()
    sync = ledger_mod.Sync(ws, ident.name, ident.github)
    try:
        if is_owner(ws, ident):
            sync.define("define the index")
        state = sync.connect(url)
    except ledger_mod.SyncError as err:
        fail(str(err))
    console.print(f"[{theme.TE}]This index {state}[/] at {url}. Others get it with: hm open {url}", highlight=False)


@app.command("entry")
def index_entry(workspace: Optional[Path] = Workspace):
    """Print this index's entry for the list of indexes, to add to indexes.toml in a pull request to Harbor-Master."""
    ws = need_workspace(workspace)
    cfg = config_mod.load(ws)
    remote = ledger_mod.Sync(ws)._git("remote", "get-url", "origin", check=False).stdout.strip() if (ws / ".git").exists() else ""
    owners = ledger_mod.owner_keys(ledger_mod.load_contributors(ws))
    if not (cfg.about.name and remote and owners):
        fail("An index can be listed once it has a name in about.toml, an owner with a key, and a repository it is "
             "shared through.")
    web = re.sub(r"^(?:ssh://)?git@([^:/]+)[:/]", r"https://\1/", remote)     # an address anyone can clone
    entry = {"name": cfg.about.name, "url": web, "owner": owners[0] if len(owners) == 1 else owners,
             "listed": time.strftime("%Y-%m-%d")}
    console.print(tomli_w.dumps({"index": [entry]}).strip(), markup=False, highlight=False, soft_wrap=True)
    console.print(f"[{theme.MU}]Add this to indexes.toml in a pull request to Harbor-Master. Its check clones {web}, so the "
                  "repository must be readable by anyone.[/]")


def clone(target: str, directory: Path | None = None, list_: str | None = None) -> Path:
    """Get an index onto this machine: clone its repository, by its name on the list of indexes or by its address,
    and check that it is an index. Returns its folder."""
    ident = need_identity()
    entry = None
    if re.search(r"[/:@]", target) or Path(target).exists():
        url = target
    else:
        entries = read_list(list_)
        entry = ledger_mod.listing.find(entries, target)
        if entry is None:
            listed = ", ".join(e["name"] for e in entries if e.get("name")) or "none yet"
            fail(f"{target} is not on the list of indexes ({listed}). See: hm indexes. An address works too: hm open URL")
        url = entry["url"]
    ws = (directory or Path(re.split(r"[/:]", url.rstrip("/").removesuffix(".git"))[-1])).resolve()
    if ws.exists() and any(ws.iterdir()):
        fail(f"{ws} is not empty.")
    try:
        ledger_mod.Sync.clone(url, ws, ident.name, ident.github)
    except ledger_mod.SyncError as err:
        fail(str(err))
    why = config_mod.is_index(ws)
    if why:
        shutil.rmtree(ws)
        fail(f"Refused: {url} is not an index, because {why}. Nothing of it was kept.")
    if entry is not None and not ledger_mod.listing.owner_matches(entry, ws):
        now = ", ".join(ledger_mod.owner_keys(ledger_mod.load_contributors(ws))) or "(none)"
        shutil.rmtree(ws)               # nothing of it is kept: its owner is not the one that was listed
        fail(f"Refused: the repository listed as {target} now names a different owner than its entry on the list.\n"
             f"  on the list:       {', '.join(ledger_mod.listing.owners(entry))}\n  in the repository: {now}\n"
             "The clone was removed. Tell the maintainers of the list.")
    set_up_local(ws)
    check_owner(ws)
    _describe(ws)
    me = next((c for c in ledger_mod.load_contributors(ws) if c.get("github", "").lower() == ident.github.lower()), None)
    if me is None or not me.get("key"):
        console.print(f"[{theme.AM}]@{ident.github} is not on this index's contributor list yet.[/] Send its owner this line. "
                      "They paste it in a shell, inside the index's folder:")
        console.print(ident.line(), markup=False, highlight=False, soft_wrap=True)
    else:
        console.print(f"[{theme.TE}]You are on the contributor list:[/] {ledger_mod.status(me)}.")
    return ws


contributors_app = typer.Typer(invoke_without_command=True, help="Who may contribute to this index, and adding someone.")
app.add_typer(contributors_app, name="contributors")


def _change_list(ws: Path, ident, people: list[dict], what: str) -> None:
    """Write the contributor list, sign it as the owner, and share both. No lock is involved."""
    (ws / ledger_mod.CONTRIBUTORS).write_text(templates.read("contributors.toml") + tomli_w.dumps({"contributor": people}))
    ledger_mod.sign_list(ws, ident, config_mod.load(ws).protocol, what)
    sync = ledger_mod.Sync(ws, ident.name, ident.github)
    try:
        sync.define(what, (ledger_mod.CONTRIBUTORS,))
        if sync.enabled:
            sync.publish(what)
    except ledger_mod.SyncError as err:
        fail(str(err))


@contributors_app.command("add")
def contributors_add(handle: str = typer.Argument(..., help="Their GitHub handle."),
                     key: list[str] = typer.Argument(..., help="Their public key, as in the line that hm init printed for them."),
                     name: str = typer.Option("", help="Their name."), affiliation: str = typer.Option(""),
                     email: str = typer.Option(""),
                     role: str = typer.Option("", help="evaluator: they only run the released index's evaluation. Default: they can curate too."),
                     workspace: Optional[Path] = Workspace):
    """Owner only: add someone to this index, by their GitHub handle and public key. It takes effect at once."""
    ws, ident = need_workspace(workspace), need_identity()
    if not is_owner(ws, ident):
        fail(f"Only the owner adds a contributor, and @{ident.github} is not the owner of this index.")
    handle, words = handle.lstrip("@"), " ".join(key).split()
    public = " ".join(words[:2])
    if not public.startswith("ssh-ed25519 ") or len(words) < 2:
        fail("That is not a public key. It is in the line that hm init printed for them, and starts with ssh-ed25519.")
    said = [part.strip() for part in " ".join(words[2:]).split("|")] + ["", "", ""]      # name | affiliation | email, when the line has them
    name, affiliation, email = name or said[0], affiliation or said[1], email or said[2]
    people = ledger_mod.load_contributors(ws)
    taken = next((c for c in people if c.get("key") == public and c.get("github") != handle), None)
    if taken:
        fail(f"That key is @{taken.get('github', '?')}'s already.")
    old = next((c for c in people if c.get("github", "").lower() == handle.lower()), None)
    entry = {**(old or {}), "name": name or (old or {}).get("name", handle), "github": handle, "key": public,
             "added": (old or {}).get("added") or time.strftime("%Y-%m-%d")}
    if affiliation:
        entry["affiliation"] = affiliation
    if email:
        entry["email"] = email
    if role == "evaluator":
        entry["role"] = role
    elif role:
        fail("The only role given here is evaluator. A lead is named with: hm contributors role HANDLE lead")
    _change_list(ws, ident, [entry if c is old else c for c in people] if old else [*people, entry], f"add contributor {handle}")
    console.print((f"[{theme.TE}]@{handle} can evaluate this index now[/]: take tasks from its to-do list and run them, once it is released. "
                   if role else f"[{theme.TE}]@{handle} can contribute to this index now[/]: add benchmarks, run them, and sign verdicts. ") +
                  "They get it with hm sync. Give them write access to the index's repository too.", highlight=False)


@contributors_app.command("role")
def contributors_role(handle: str = typer.Argument(..., help="Their GitHub handle."),
                      role: str = typer.Argument(..., help="lead, evaluator, or contributor to take the role away."),
                      workspace: Optional[Path] = Workspace):
    """Owner only: make someone a lead, who makes the final cut with the owner, an evaluator, who only runs the
    released index's evaluation, or an ordinary contributor again."""
    ws, ident = need_workspace(workspace), need_identity()
    if not is_owner(ws, ident):
        fail(f"Only the owner names a lead, and @{ident.github} is not the owner of this index.")
    if role not in ("lead", "contributor", "evaluator"):
        fail("The role is lead, contributor or evaluator.")
    handle, people = handle.lstrip("@"), ledger_mod.load_contributors(ws)
    who = next((c for c in people if c.get("github", "").lower() == handle.lower()), None)
    if who is None or who.get("role") == "owner":
        fail(f"@{handle} is the owner." if who else f"@{handle} is not on the list. Add them first: hm contributors add HANDLE KEY")
    changed = {k: v for k, v in who.items() if k != "role"} | ({"role": role} if role != "contributor" else {})
    _change_list(ws, ident, [changed if c is who else c for c in people], f"make {handle} a {role}")
    console.print(f"[{theme.TE}]@{handle} is a lead now[/]: with the owner, they make the final cut of accepted tasks (hm cut TASK why)."
                  if role == "lead" else f"@{handle} is an evaluator now: they run the released index's evaluation (hm eval), and nothing else."
                  if role == "evaluator" else f"@{handle} is an ordinary contributor again.", highlight=False)


@contributors_app.command("remove")
def contributors_remove(handle: str, workspace: Optional[Path] = Workspace):
    """Owner only: take someone off this index's contributor list. Their earlier records stay."""
    ws, ident = need_workspace(workspace), need_identity()
    if not is_owner(ws, ident):
        fail(f"Only the owner changes the contributor list, and @{ident.github} is not the owner of this index.")
    handle, people = handle.lstrip("@"), ledger_mod.load_contributors(ws)
    gone = next((c for c in people if c.get("github", "").lower() == handle.lower()), None)
    if gone is None or gone.get("role") == "owner":
        fail(f"@{handle} is the owner." if gone else f"@{handle} is not on the list. See: hm contributors")
    _change_list(ws, ident, [c for c in people if c is not gone], f"remove contributor {handle}")
    console.print(f"Took @{handle} off the contributor list. Records they signed after this are refused.", highlight=False)


@contributors_app.callback()
def contributors(ctx: typer.Context, workspace: Optional[Path] = Workspace):
    """Who may contribute to this index, and who on the list still needs a handle or a key."""
    if ctx.invoked_subcommand:
        return
    ws = need_workspace(workspace)
    people = ledger_mod.load_contributors(ws)
    tb = Table(header_style=theme.MU, border_style=theme.BORDER)
    for col in ("name", "affiliation", "GitHub", "role", "status"):
        tb.add_column(col)
    for c in people:
        ok = bool(c.get("github") and c.get("key"))
        tb.add_row(c.get("name", "?"), c.get("affiliation", ""), f"@{c['github']}" if c.get("github") else "-",
                   c.get("role", ""), f"[{theme.TE if ok else theme.AM}]{ledger_mod.status(c)}[/]")
    console.print(tb)
    console.print(f"[{theme.MU}]Everyone on it may add benchmarks, run them and sign verdicts. The owner and the leads make the "
                  "final cut. The owner adds someone with hm contributors add, and names a lead with "
                  "hm contributors role HANDLE lead[/]")


def keys_file() -> Path:
    """The keys file, made from the example the first time: in the repository folder, readable only by you, ignored by git."""
    path = keys.secrets_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        keys.ensure_ignored(path)
        path.touch(mode=0o600)
        path.write_text(templates.read("secrets.env.example"))
    return path
