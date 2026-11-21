"""Setting up: identity, an index to work on, keys."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
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
from .listing import List, read_list
from .common import Workspace, app, check_owner, console, fail, is_owner, need_identity, need_workspace, set_up_local


@app.command()
def init(name: str = typer.Option(None), affiliation: str = typer.Option(None), email: str = typer.Option(None),
         github: str = typer.Option(None, help="GitHub handle. It identifies you in the ledger.")):
    """Everyone starts here. It asks who you are and creates your signing key, then says how to start, on demo-index.
    One box at a time. Run it again to change your details: each box shows
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
    console.print(f"[{theme.MU}]hm indexes lists the indexes, and hm clone INDEX clones one.[/]")


def _skills_line(ws: Path) -> str:
    states = [state for _, state in skills.status(ws / "skills")]
    own, absent = states.count("own"), states.count("missing")
    if absent:
        return f"{absent} of {len(states)} not in the index yet"
    return f"{len(states)} in the index, " + (f"{own} in its own wording" if own else "all as the templates have them")


def _describe(ws: Path) -> None:
    """What an index is: what `hm clone INDEX` prints."""
    cfg = config_mod.load(ws)
    led = ledger_mod.Ledger(ws)
    people = ledger_mod.load_contributors(ws)
    owners = [f"{c.get('name', '?')} (@{c['github']})" for c in people if c.get("role") == "owner" and c.get("github")]
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
            ("version", f"v{cfg.about.version}, settings {cfg.protocol}"),
            ("skills", _skills_line(ws)),
            ("shared at", remote or "nowhere yet"),
            ("folder", str(ws))]
    for label, value in rows:
        console.print(f"  [{theme.MU}]{label:<15}[/]{value}")


def store_problem(tasks: str) -> str:
    """Why this person cannot use the task store an index is to have, or "": a folder must be writable, and on Hugging Face
    their HF_TOKEN must write to the dataset, or create it where it does not exist yet."""
    from ..tasks import store
    where = tasks.strip()
    if where.startswith(("/", "~", ".")):
        folder = Path(where).expanduser()
        existing = next((p for p in (folder, *folder.parents) if p.exists()), folder)
        return "" if os.access(existing, os.W_OK) else f"The task store {folder} cannot be written to here."
    if not keys.get("HF_TOKEN"):
        return (f"The task store is the Hugging Face dataset {where}, and making it needs your own HF_TOKEN, with write access. "
                "Give it for this command: HF_TOKEN=hf_... hm new ...  Once the index is made, it goes in its secrets.env.")
    try:
        there = store.HuggingFace(where)
        return there.writable() if there.exists() else there.creatable()
    except store.StoreError as err:
        return str(err)


def repo_problem(addresses: list[str]) -> tuple[str, str]:
    """Which of these addresses of a repository works, and why none does: it must be reachable with this machine's own git
    credentials, empty, and accept a push. The push is tried as a dry run, from a throwaway repository, so nothing is sent.
    Returns (the address that works, ""), or ("", why)."""
    import tempfile
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}        # never wait for a password typed in a shell

    def said(r: subprocess.CompletedProcess) -> str:        # git's first line that says what went wrong
        lines = [line.strip() for line in r.stderr.splitlines() if line.strip()]
        return next((line for line in lines if line.startswith(("fatal:", "ERROR:", "error:", "remote:"))), lines[-1] if lines else "git failed")

    git = lambda *a, cwd=None: subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, timeout=90, env=env)  # noqa: E731
    failed: list[str] = []
    for address in dict.fromkeys(a for a in addresses if a):
        try:
            seen = git("ls-remote", address)
            if seen.returncode:
                failed.append(f"{address} could not be read: {said(seen)}")
                continue
            if seen.stdout.strip():
                return "", f"{address} is not empty. An index starts in an empty repository: create a new one, with no README."
            with tempfile.TemporaryDirectory() as tmp:
                git("init", "--quiet", "-b", "main", cwd=tmp)
                git("-c", "user.name=hm", "-c", "user.email=hm@localhost", "commit", "--quiet", "--allow-empty", "-m", "check", cwd=tmp)
                pushed = git("push", "--dry-run", "--quiet", address, "HEAD:refs/heads/main", cwd=tmp)
            if pushed.returncode == 0:
                return address, ""
            failed.append(f"{address} does not take a push from you: {said(pushed)}")
        except subprocess.TimeoutExpired:
            failed.append(f"{address} did not answer.")
    return "", "; ".join(failed) or "No address of the repository was given."


def index_new(name: str, directory: Path | None = None, title: str = "", description: str = "", tasks: str = "", url: str = "",
              trial: bool = False) -> Path:
    """Start a new index with you as its owner: the files that define it, in a git repository of its own. What hm new does,
    and, with `trial`, an index for made-up tasks only, which keeps none."""
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
    web = re.sub(r"^(?:git@github\.com:|https://github\.com/)(.+?)(?:\.git)?$", r"https://github.com/\1", url)    # what anyone can clone
    folder = re.split(r"[/:]", (web or name).rstrip("/").removesuffix(".git"))[-1]      # the folder hm clone makes of it
    store_at = f"the dataset `{tasks}` on Hugging Face" if tasks and not tasks.startswith(("/", "~", ".")) else "the task store"
    fill = lambda text: Template(text).substitute(name=name, title=title or name, canary=uuid.uuid4(), clone=web or name,  # noqa: E731
                                                  folder=folder, store=store_at)
    (ws / "protocol.toml").write_text(templates.read("protocol.toml"))
    about = fill(templates.read("about.toml"))
    for key, value in (("description", description), ("tasks", tasks)):
        about = about.replace(f'{key} = ""', f"{key} = {json.dumps(value.strip(), ensure_ascii=False)}")
    (ws / config_mod.ABOUT_NAME).write_text(about)
    (ws / "README.md").write_text(fill(templates.read("index-readme.md")))
    (ws / ".gitignore").write_text(templates.read("gitignore"))
    (ws / config_mod.CONFIG_EXAMPLE).write_text(templates.read("hm.toml"))             # what each clone's hm.toml and
    (ws / config_mod.SECRETS_EXAMPLE).write_text(templates.read("secrets.env.example"))   # secrets.env are made from
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
            state = ledger_mod.Sync(ws, ident.name, ident.github).connect(url, fresh=True)
            console.print(f"[{theme.TE}]It {state}[/] at {url}. Others get it with: hm clone {url}", highlight=False)
        except ledger_mod.SyncError as err:
            console.print(f"[{theme.AM}]It is not shared yet:[/] {err}\nThe index itself is fine. Once the repository exists and is "
                          f"empty, inside the index's folder: git remote add origin {url}. The next hm command there shares it.", highlight=False)
    console.print(f"[{theme.MU}]Next: cd {ws.name}. Edit about.toml, protocol.toml and skills/ as you want them: "
                  "your next hm command shares them. hm contributors add lets someone in, hm curate curates it, and hm view shows where it stands.[/]", highlight=False)
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
    console.print(f"[{theme.TE}]This index {state}[/] at {url}. Others get it with: hm clone {url}", highlight=False)


def entry_of(ws: Path) -> tuple[dict, list[str]]:
    """This index's entry for the list of indexes, and what keeps it from being listed, checked here before the list's
    own check clones it."""
    cfg = config_mod.load(ws)
    sync = ledger_mod.Sync(ws)
    at = sync.where()
    owners = ledger_mod.owner_keys(ledger_mod.load_contributors(ws))
    web = re.sub(r"^(?:ssh://)?git@([^:/]+)[:/]", r"https://\1/", at["remote"])     # an address anyone can clone
    entry = {"name": cfg.about.name, "url": web, "owner": owners[0] if len(owners) == 1 else owners, "listed": time.strftime("%Y-%m-%d")}
    wrong = [p for p in (config_mod.is_index(ws),) if p]
    if not ledger_mod.listing.NAME.fullmatch(cfg.about.name or ""):
        wrong.append("about.toml has no name of lower case letters, digits and hyphens")
    if not (cfg.about.description or "").strip():
        wrong.append("about.toml has no description: say what the index measures, and for whom")
    if not (cfg.about.tasks or "").strip():
        wrong.append("about.toml names no task store")
    if not owners:
        wrong.append("the contributor list has no owner with a key")
    if not at["remote"]:
        wrong.append("it is shared through no repository: git remote add origin URL")
    elif at["unpushed"] not in ("", "0") or at["committed"] != "yes":
        wrong.append("this folder has records that could not be pushed: check that this clone can push to the index's repository")
    wrong += skills.problems(ws / "skills") + [f"it has no copy of skills/{n}" for n in skills.missing(ws / "skills")]
    wrong += ledger_mod.verify(ws)
    return entry, wrong


@app.command()
def publish(list_: str = List, workspace: Optional[Path] = Workspace):
    """Owner only: check that this index is sound, then ask Harbor-Master to list it, in a pull request that adds its entry
    to indexes.toml. The pull request's own check clones the repository, so it must be readable by anyone."""
    import tempfile
    ws, ident = need_workspace(workspace), need_identity()
    if not is_owner(ws, ident):
        fail(f"Only the owner publishes an index, and @{ident.github} is not the owner of this one.")
    entry, wrong = entry_of(ws)
    listed = read_list(list_)
    same = next((e for e in listed if e.get("name") == entry["name"]), None)
    if same and ledger_mod.listing._same_place(same["url"]) == ledger_mod.listing._same_place(entry["url"]):
        console.print(f"{entry['name']} is listed already, at {same['url']}. Nothing to do.", highlight=False)
        return
    if same:
        wrong.append(f"the name {entry['name']} is listed already, for {same['url']}: give this index another name in about.toml")
    if not wrong:               # what the list's check does: clone the address as anyone would, and look at what it holds
        with tempfile.TemporaryDirectory() as tmp:
            _, found = ledger_mod.listing.inspect(entry, Path(tmp) / "index")
        wrong += [f"its repository, as anyone clones it: {p}" for p in found]
    if wrong:
        for p in wrong[:30]:
            console.print(f"[{theme.RD}]x[/] {p}", highlight=False)
        fail(f"Not published: {len(wrong)} thing{'s' if len(wrong) != 1 else ''} to put right first.")
    text = tomli_w.dumps({"index": [entry]}).strip()
    console.print(f"[{theme.TE}]{entry['name']} is a sound index.[/] Its entry:\n{text}", highlight=False)
    repo = ledger_mod.listing.REPO
    if not shutil.which("gh"):
        console.print(f"To list it, open https://github.com/{repo}/edit/main/indexes.toml, add those lines at the end, and propose "
                      "the change: GitHub makes the pull request. Or install the GitHub CLI (gh) and run hm publish again.", highlight=False)
        return
    if sys.stdin.isatty() and not typer.confirm(f"Open a pull request to {repo} that adds this entry?", default=True):
        return
    branch = f"list-{entry['name']}"
    with tempfile.TemporaryDirectory() as tmp:
        run = lambda *args, cwd=tmp: subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
        try:
            login = run("gh", "api", "user", "-q", ".login")
            run("gh", "repo", "fork", repo, "--clone", "--default-branch-only", "--", "--depth", "1", "fork")
            fork = str(Path(tmp) / "fork")
            run("git", "fetch", "-q", "--depth", "1", "upstream", "main", cwd=fork)     # a fork made long ago lags behind
            run("git", "checkout", "-q", "-b", branch, "FETCH_HEAD", cwd=fork)
            path = Path(fork) / "indexes.toml"
            path.write_text(path.read_text().rstrip() + "\n\n" + text + "\n")
            run("git", "commit", "-q", "-am", f"List the {entry['name']} index", cwd=fork)
            run("git", "push", "-q", "--force", "origin", branch, cwd=fork)
            url = run("gh", "pr", "create", "--repo", repo, "--base", "main", "--head", f"{login}:{branch}",
                      "--title", f"List the {entry['name']} index", "--body",
                      f"Adds {entry['name']} ({entry['url']}) to the list of indexes, as `hm publish` checked it.", cwd=fork)
        except (OSError, subprocess.CalledProcessError) as err:
            fail(f"The pull request could not be made: {(getattr(err, 'stderr', '') or str(err)).strip()[:300]}")
    console.print(f"[{theme.TE}]Asked to be listed:[/] {url}. It is listed once that is merged, after its check passes.", highlight=False)


DEMO = "demo-index"
DEMO_CANARY = "5c0e9a52-7d41-4b8e-9f3a-2e6b1d8c4a70"
DEMO_ABOUT = ("A made-up index, to try Harbor-Master on: hm curate --demo and hm view --demo, all simulated. "
              "Whoever makes it is its owner. Its benchmark is made up on your machine, nothing is spent, and nothing of it is shared.")


def make_demo(ws: Path) -> Path:
    """demo-index: an index that ships with the tool, so that it can be cloned with no repository and no network.
    Whoever makes it is its owner, as with hm new, beside three made-up contributors whose keys are made here, in .hm. It is
    meant for simulated runs: it names no task store and lists no benchmark."""
    ident = need_identity()
    if ws.exists() and any(ws.iterdir()):
        fail(f"{ws} is not empty.")
    ws.mkdir(parents=True, exist_ok=True)
    fill = lambda text: Template(text).substitute(name=DEMO, title="Demo Index", canary=DEMO_CANARY)  # noqa: E731
    (ws / config_mod.ABOUT_NAME).write_text(fill(templates.read("about.toml")).replace('description = ""', f"description = {json.dumps(DEMO_ABOUT)}"))
    (ws / "protocol.toml").write_text(templates.read("protocol.toml"))
    from .home import demo_people
    (ws / ledger_mod.CONTRIBUTORS).write_text(templates.read("contributors.toml") + tomli_w.dumps({"contributor": [{
        "name": ident.name, "affiliation": ident.affiliation, "email": ident.email, "github": ident.github,
        "key": ident.public_key, "role": "owner", "added": time.strftime("%Y-%m-%d")}, *demo_people(ws)]}))
    (ws / "README.md").write_text(f"# Demo Index\n\n{DEMO_ABOUT}\n")
    (ws / ".gitignore").write_text(templates.read("gitignore"))
    skills.adopt(ws / "skills", skills.names())
    ledger_mod.sign_list(ws, ident, config_mod.load(ws).protocol, "start the demo index")
    try:
        ledger_mod.Sync(ws, ident.name, ident.github).define("start the demo index")
        ledger_mod.Sync(ws, ident.name, ident.github)._commit("sign the contributor list")
    except ledger_mod.SyncError as err:
        fail(str(err))
    set_up_local(ws)
    check_owner(ws)
    console.print(f"Made [bold]{DEMO}[/] in {ws}, with you as its owner: a made-up index to try Harbor-Master on, simulated, "
                  "on this machine only.", highlight=False)
    return ws


def clone(target: str, directory: Path | None = None, list_: str | None = None) -> Path:
    """Get an index onto this machine: clone its repository, by its name on the list of indexes or by its address,
    and check that it is an index. Returns its folder. demo-index is made from the tool's own files."""
    if target == DEMO and not Path(target).exists():
        return make_demo((directory or Path(DEMO)).resolve())
    ident = need_identity()
    entry = None
    if re.search(r"[/:@]", target) or Path(target).exists():
        url = target
    else:
        entries = read_list(list_)
        entry = ledger_mod.listing.find(entries, target)
        if entry is None:
            listed = ", ".join(e["name"] for e in entries if e.get("name")) or "none yet"
            fail(f"{target} is not on the list of indexes ({listed}). See: hm indexes. An address works too: hm clone URL")
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
    """Write the contributor list, sign it as the owner, and share both."""
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
    _change_list(ws, ident, [entry if c is old else c for c in people] if old else [*people, entry], f"add contributor {handle}")
    console.print(f"[{theme.TE}]@{handle} can contribute to this index now[/]: add benchmarks, curate them, and sign verdicts. " +
                  "It reaches them with their next hm command. Give them write access to the index's repository too.", highlight=False)


@contributors_app.command("role")
def contributors_role(handle: str = typer.Argument(..., help="Their GitHub handle."),
                      role: str = typer.Argument(..., help="lead, or contributor to take the role away."),
                      workspace: Optional[Path] = Workspace):
    """Owner only: make someone a lead, who makes the final cut with the owner, or an ordinary contributor again."""
    ws, ident = need_workspace(workspace), need_identity()
    if not is_owner(ws, ident):
        fail(f"Only the owner names a lead, and @{ident.github} is not the owner of this index.")
    if role not in ("lead", "contributor"):
        fail("The role is lead, or contributor.")
    handle, people = handle.lstrip("@"), ledger_mod.load_contributors(ws)
    who = next((c for c in people if c.get("github", "").lower() == handle.lower()), None)
    if who is None or who.get("role") == "owner":
        fail(f"@{handle} is the owner." if who else f"@{handle} is not on the list. Add them first: hm contributors add HANDLE KEY")
    changed = {k: v for k, v in who.items() if k != "role"} | ({"role": role} if role != "contributor" else {})
    _change_list(ws, ident, [changed if c is who else c for c in people], f"make {handle} a {role}")
    console.print(f"[{theme.TE}]@{handle} is a lead now[/]: with the owner, they make the final cut of accepted tasks (hm cut TASK why)."
                  if role == "lead" else f"@{handle} is an ordinary contributor again.", highlight=False)


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
    """The keys file, made from the example the first time: in the index folder, readable only by you, ignored by git."""
    path = keys.secrets_path()
    if path is None:
        fail("Keys are kept in an index's secrets.env: cd into the index's folder first.")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        keys.ensure_ignored(path)
        path.touch(mode=0o600)
        path.write_text(templates.read("secrets.env.example"))
    return path
