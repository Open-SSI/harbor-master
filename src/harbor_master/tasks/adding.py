"""Adding a benchmark to an index's list (DESIGN.md Section 9). The person gives whatever they have; the worker agent
searches the web for its repository and what is known about it (find); the code pins that repository's release,
downloads it and finds its tasks (from_found); the person checks the draft once, or corrects it (direct, refine); and
then it is a signed record (confirm). The curate screen's box and `hm benchmarks add` both use these.

In a simulated run there is no agent to search with, so a stand-in makes the benchmark up from what was given, with
made-up tasks on this machine, and it is kept off the index's list (made_up, add_made_up)."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from .. import ledger as ledger_mod
from ..core import config as config_mod
from . import supported

DRAFT = "benchmark-draft.json"      # in .hm: the entry being added, until it is confirmed


class AddError(Exception):
    """Why a benchmark cannot be added, said so that a person knows what to do."""


def repo_key(link: str) -> str:
    """One spelling for one repository, so that the same one given another way is still found: with or without
    https:// or www., as git@github.com:, with .git or a slash at the end, with /tree/... after it, or as just owner/name
    for GitHub. A folder on this machine is its full path."""
    s = link.strip()
    if s.startswith(("/", ".", "~")):
        return os.path.normpath(os.path.expanduser(s))
    s = s.lower()
    if re.fullmatch(r"[\w.-]+/[\w.-]+/?", s) and "." not in s.split("/")[0]:
        s = "github.com/" + s
    s = re.sub(r"^(git\+)?(ssh://)?git@([^:/]+)[:/]", r"\3/", s)
    s = re.sub(r"^(git\+)?(https?|ssh|git)://", "", s).removeprefix("www.")
    parts = [x for x in s.split("/") if x]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1].removesuffix(".git")
    keep = {"github.com": 3, "huggingface.co": 4 if parts[1:2] == ["datasets"] else 3}.get(parts[0] if parts else "", len(parts))
    return "/".join(p.removesuffix(".git") for p in parts[:keep])


def _same(a: str, b: str) -> bool:
    return bool(a and b) and repo_key(a) == repo_key(b)


def on_the_list(ws: Path, link: str, made_up: bool = False) -> str:
    """The benchmark that a repository link already is: on this index's list, dropped from it, or (with `made_up`, in a
    simulated run) one of the made-up benchmarks registered here. Its ID, or "" when it is new."""
    events = ledger_mod.Ledger(ws).events
    found = next((e["id"] for e in supported.supported(ws) if _same(e.get("github", ""), link)), "")
    if found:
        return found
    added = {e["entry"]["id"]: e["entry"].get("github", "") for e in events
             if e["type"] == "benchmark" and e.get("value") == "add" and isinstance(e.get("entry"), dict)}
    found = next((b for b in ledger_mod.abandoned(events) if _same(added.get(b, ""), link)), "")
    if found or not made_up:
        return found
    from .registry import read_registry
    return next((b for b, info in read_registry(ws).items() if _same(info.get("github", ""), link)), "")


def may_add(ws: Path, ident) -> None:
    if not any(c.get("github") == ident.github and ledger_mod.can_contribute(c) for c in ledger_mod.load_contributors(ws)):
        raise AddError(f"@{ident.github} is not on this index's contributor list yet, so cannot add a benchmark to it.")


def clash(ws: Path, name: str, github: str) -> None:
    """Refuse a benchmark that is on the list already, by its repository, whatever it stands at: a dropped one is curated
    again by its name, not added again."""
    found = next((e for e in supported.supported(ws) if _same(e["github"], github)), None)
    if found:
        raise AddError(f"That repository is on this index's list already, as {found['id']}, added by @{found.get('added_by', '?')} "
                       f"on {found.get('added', '?')}.")
    found = supported.find(ws, name)
    if found:
        raise AddError(f"{found['id']} is on this index's list already, for another repository ({found['github']}). "
                       "Give this one another name.")


def draft(ws: Path, ident, url: str, ref: str = "", id_: str = "") -> dict:
    """The entry as the code can read it from the repository: pinned, with its tasks found and its licence read."""
    from ..backends import describe
    may_add(ws, ident)
    github = url.strip().rstrip("/").removesuffix(".git")
    sync = ledger_mod.Sync(ws, ident.name, ident.github)
    try:
        if sync.enabled:            # the list as everyone has it now, so that what someone else just added is seen
            sync.pull()
        clash(ws, id_ or github, github)
        ref, commit = supported.pin(url.strip(), ref)
        entry = supported.describe(url.strip(), ref, commit, config_mod.local(ws) / "sources", id_)
    except (ValueError, subprocess.CalledProcessError, ledger_mod.SyncError) as err:
        raise AddError(str(getattr(err, "stderr", "") or err).strip()) from None
    entry["pinned_on"] = entry["released"]          # until someone says when it came out, the pinned commit's date stands in
    describe.merge(entry, describe.guess(config_mod.local(ws) / "sources" / entry["id"]))
    return keep(ws, entry)


async def find(ws: Path, ident, given: str, entry: dict | None = None, note: str = "") -> tuple[dict, float]:
    """The worker agent searches the web for the benchmark: from a link of any kind, or a name, its repository, newest
    release and entry. With `entry`, the draft being corrected by `note`. Raises AddError when it found no repository."""
    from ..backends import NotImplementedBackend, describe
    may_add(ws, ident)
    repo = config_mod.local(ws) / "sources" / entry["id"] if entry else None
    try:
        data, cost = await describe.find(config_mod.load(ws), given, entry, repo, note)
    except (RuntimeError, NotImplementedBackend) as err:
        raise AddError(f"The agent could not search for it: {str(err)[:200]}") from None
    if not str(data.get("repository", "")).strip():
        read = ", ".join(data.get("sources") or []) or "nothing it could use"
        raise AddError(f"The agent found no repository that holds this benchmark's tasks (it read {read}). Give the link to "
                       "its repository, or a page that names it.")
    return data, cost


def from_found(ws: Path, ident, data: dict, id_: str = "") -> dict:
    """The draft of what the agent found: its repository pinned at the release it named (or the newest tag, else the main
    branch, when that tag is not there), downloaded, and its tasks found, by the code. What the agent read on the web is
    put over what the README gave away."""
    from ..backends import describe
    try:
        entry = draft(ws, ident, data["repository"].strip(), str(data.get("release", "")).strip(), id_)
    except AddError as err:
        if "has no tag or branch called" not in str(err):
            raise
        entry = draft(ws, ident, data["repository"].strip(), "", id_)
    describe.merge(entry, data)
    return keep(ws, entry)


async def refine(ws: Path, ident, entry: dict, note: str) -> tuple[dict, float]:
    """A correction in plain words: the agent searches again, with the draft and the repository as downloaded. When the
    person points it at another repository, the draft starts again from that one."""
    from ..backends import describe
    data, cost = await find(ws, ident, entry.get("github", ""), entry, note)
    if not _same(data["repository"], entry.get("github", "")):
        return from_found(ws, ident, data), cost
    describe.merge(entry, data)
    return keep(ws, entry), cost


def _slug(given: str) -> str:
    words = re.sub(r"^\w+://|^www\.", "", given.strip().lower()).rstrip("/").removesuffix(".git")
    parts = [p for p in re.split(r"[/?#=&]+", words) if p and p not in ("tree", "main", "master", "blob", "abs", "pdf", "datasets")]
    name = re.sub(r"[^a-z0-9]+", "-", parts[-1] if parts else "benchmark").strip("-") or "benchmark"
    name = f"paper-{name}" if name[:1].isdigit() else name
    return name if "bench" in name else f"{name}-bench"


QUIZ = re.compile(r"quiz|trivia|mcq|-qa\b|qa-")      # a made-up benchmark of quiz questions, to show a rejection


def made_up(ws: Path, given: str) -> dict:
    """In a simulated run, what the agent would have found, made up from what was given: a benchmark of 8 made-up tasks,
    complete, at a made-up repository, with its files downloaded here as a real one's would be, so that the checks read
    them. A link whose name says quiz, trivia, mcq or qa is made up as a benchmark of quiz questions, which the checks
    reject. It is registered for runs only when it is added."""
    import time

    from ..cli.dev import fake_benchmark
    from .registry import read_registry
    name = _slug(given)
    if name in read_registry(ws) or supported.find(ws, name):
        raise AddError(f"{name} is on this index's list already. Give it another link.")
    paper = given.strip() if re.search(r"arxiv\.org|doi\.org|openreview\.net", given) else "none"
    quiz = bool(QUIZ.search(name))
    sources = config_mod.local(ws) / "sources"
    if not (sources / name).is_dir():
        fake_benchmark(name, 8, directory=sources, seed=sum(map(ord, name)))
    return {"id": name, "name": name.replace("-", " ").title(), "made_up": True,
            "task_kind": "question_answering" if quiz else "agentic", "grading": "programmatic", "solutions": "all",
            "access": "public", "best_reported": 0.62 if quiz else 0.18,
            "assessed_from": "made up by the simulated agent: nothing was read",
            "description": f"Made up by the simulated agent from what you gave ({given.strip()}). Nothing in it is real.",
            "paper": paper, "released": time.strftime("%Y-%m-%d"), "license": "MIT", "homepage": "",
            "github": f"https://github.com/example/{name}", "ref": "v1.0", "commit": "0" * 40, "version": "1.0",
            "tasks": ".", "task_count": 8, "format": "harbor"}


def add_made_up(ws: Path, entry: dict) -> dict:
    """Make the made-up benchmark's tasks on this machine and register them for simulated runs. It is not signed onto
    the index's list: nothing of a simulated run enters the index."""
    from .registry import read_registry, register
    if entry["id"] in read_registry(ws):
        raise AddError(f"{entry['id']} is on this index's list already.")
    register(ws, entry["id"], source_of(ws, entry), dev=True, github=entry["github"])
    return entry


def source_of(ws: Path, entry: dict) -> Path:
    """Where a drafted benchmark's release was downloaded: what the checks read."""
    return config_mod.local(ws) / "sources" / entry["id"]


def direct(ws: Path, entry: dict, text: str) -> bool:
    """A correction written as `field: value`. False when it is not one, and an agent must read it."""
    from ..backends import describe
    if describe.direct(entry, text):
        keep(ws, entry)
        return True
    return False


def keep(ws: Path, entry: dict) -> dict:
    path = config_mod.local(ws) / DRAFT
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(entry, indent=2) + "\n")
    return entry


def kept(ws: Path) -> dict | None:
    path = config_mod.local(ws) / DRAFT
    return json.loads(path.read_text()) if path.exists() else None


def lacking(entry: dict) -> list[str]:
    return ledger_mod.benchmarks.missing(entry)


def table(rows: list[tuple[str, str]], label_style: str, missing_style: str, missing: str = "not found", indent: int = 0):
    """Rows of (field, value) for a person to read: the fields in a column of their own, and a long value wrapped
    under its own first word, never back under the field names."""
    from rich.table import Table
    g = Table.grid(padding=(0, 0, 0, indent))
    g.add_column(width=13, no_wrap=True, style=label_style)
    g.add_column(ratio=1, overflow="fold")
    for key, value in rows:
        g.add_row(key, value if value else f"[{missing_style}]{missing}[/]")
    return g


def lines(entry: dict) -> list[tuple[str, str]]:
    """The draft as (field, value) rows, for a person to check."""
    rows = [(key, str(entry.get(key, "") or "")) for key in ("name", "description", "paper", "released", "license")]
    rows.append(("pinned", f"{entry.get('ref', '')} ({str(entry.get('commit', ''))[:10]}), from {entry.get('github', '')}"))
    under = "at its top" if entry.get("tasks") in (".", "") else f"under {entry.get('tasks')}"
    rows.append(("tasks", f"{entry.get('task_count')} Harbor tasks {under}" if entry.get("format") == "harbor"
                 else "not Harbor task folders: the first run writes an adapter"))
    return rows


def confirm(ws: Path, ident, entry: dict) -> dict:
    """Add the benchmark to the index's list, as a record signed with the person's key, and share it."""
    may_add(ws, ident)
    missing = lacking(entry)
    if missing:
        raise AddError(f"it still needs {', '.join(missing)}.")
    sync = ledger_mod.Sync(ws, ident.name, ident.github)
    try:
        if sync.enabled:
            sync.pull()
        clash(ws, entry["id"], entry["github"])
        entry = {k: v for k, v in entry.items() if k != "pinned_on"}
        ledger_mod.benchmarks.add(ws, ident, config_mod.load(ws).protocol, entry)
        ledger_mod.benchmarks.write_list(ws)
        if sync.enabled:
            sync.publish(f"add benchmark {entry['id']}")
    except ledger_mod.SyncError as err:
        raise AddError(str(err)) from None
    (config_mod.local(ws) / DRAFT).unlink(missing_ok=True)
    return entry
