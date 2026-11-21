"""API keys: environment first, then the index's own secrets.env, git-ignored (DESIGN.md Section 8)."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .config import Config
from .identity import config_dir

# The variable each provider reads. Values never enter hm.toml, the ledger, or an upload.
# Each agent takes an API key, or a sign-in to a subscription. The sign-in is used when both are set.
AGENT_KEYS = {  # the only agents a trial runs, and the credentials each takes: [API key, subscription sign-in]
    "claude-code": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"],
    "codex": ["OPENAI_API_KEY", "CODEX_AUTH_JSON_PATH"],
}
PATH_KEYS = {"CODEX_AUTH_JSON_PATH"}     # the value is the path of a file that holds the credential
PROVIDER_KEYS = {
    "openrouter": ["OPENROUTER_API_KEY"],
    "claude-agent-sdk": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"],  # either one
    "codex": ["CODEX_AUTH_JSON_PATH", "OPENAI_API_KEY"],    # either one: the sign-in `codex login` writes (~/.codex/auth.json
                                                            # is found without the variable), or an API key
    "mock": [],
}
OTHER_KEYS = ["HF_TOKEN"]
KEY_PATTERN = re.compile(r"(sk-[A-Za-z0-9_\-]{20,}|hf_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}"
                         r"|eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,})")     # the last is a sign-in token


SECRETS = "secrets.env"


_index: Path | None = None       # the index a command works on, as it named it: set by need_workspace


def use(index_dir: Path) -> None:
    global _index
    _index = index_dir


def secrets_path() -> Path | None:
    """Where the keys file is: $HM_SECRETS, else the secrets.env of the index a command works on (named with -w, or the
    one this folder is in). Outside an index there is none, and keys come from the environment only. Who you are
    (HM_CONFIG_DIR) has no bearing on it: a second person on one machine has their keys in their own clone."""
    if os.environ.get("HM_SECRETS"):
        return Path(os.environ["HM_SECRETS"])
    from .config import find_workspace
    found = _index or find_workspace()
    return found / SECRETS if found else None


def ensure_ignored(path: Path) -> None:
    """Make sure git will never pick the keys file up: add it to the folder's .gitignore if it is missing."""
    ignore = path.parent / ".gitignore"
    lines = ignore.read_text().splitlines() if ignore.exists() else []
    if path.name not in [line.strip() for line in lines]:
        ignore.write_text("\n".join([*lines, path.name]) + "\n")


def _file_values() -> dict[str, str]:
    path = secrets_path()
    out: dict[str, str] = {}
    if path is not None and path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip():
                    out.setdefault(k.strip(), v.strip())  # the first occurrence wins
    return out


def get(name: str) -> str | None:
    value = os.environ.get(name) or _file_values().get(name)
    if value and name in PATH_KEYS:      # a sign-in file counts only if it is there
        path = Path(value).expanduser()
        return str(path.resolve()) if path.is_file() else None
    return value


def needs(agent: str) -> list[str]:
    """The variables that can hold an agent's credential: any one of them is enough. Only Claude Code and Codex are run."""
    return list(AGENT_KEYS.get(agent, []))


def found(agent: str) -> bool:
    """Whether an agent can run here: its API key, or its subscription's sign-in."""
    return any(get(n) for n in needs(agent)) or (agent == "codex" and codex_sign_in() is not None)


def codex_sign_in() -> Path | None:
    """The file that holds a Codex sign-in to a ChatGPT subscription: the one CODEX_AUTH_JSON_PATH names, else the
    ~/.codex/auth.json that `codex login` writes. Codex has no long-lived token to put in a variable, as Claude has:
    its sign-in is this file, which Codex refreshes itself."""
    named = get("CODEX_AUTH_JSON_PATH")
    if named:
        return Path(named)
    usual = Path.home() / ".codex" / "auth.json"
    return usual if usual.is_file() else None


def agent_env(agent: str = "") -> dict[str, str]:
    """The credentials an agent trial gets, as environment variables: Claude's and OpenAI's. No other key in
    secrets.env is passed on.

    The same rule as for the curation agents: where both an API key and a subscription's sign-in are there, the
    sign-in is used. Harbor uses a Codex sign-in only when it is named, so the one found is named for it.
    """
    env = {n: v for n in sum(AGENT_KEYS.values(), []) if (v := get(n))}
    if "CLAUDE_CODE_OAUTH_TOKEN" in env and agent in ("", "claude-code"):
        env["CLAUDE_FORCE_OAUTH"] = "1"
    found = codex_sign_in()
    if found is not None and found.is_file() and agent in ("", "codex"):
        env["CODEX_AUTH_JSON_PATH"] = str(found.resolve())
    return env


def set_key(name: str, value: str) -> None:
    path = secrets_path()
    if path is None:
        raise ValueError("Keys are kept in an index's secrets.env: this folder is not inside an index.")
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_ignored(path)
    values = _file_values()
    values[name] = value.strip()
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))


def doctor(cfg: Config) -> list[tuple[str, str, bool, str]]:
    """Rows of (what, variable, present, used for)."""
    rows = []
    for tier in ("decision", "worker", "frontier"):
        t = cfg.tier(tier)
        names = PROVIDER_KEYS.get(t.provider, [])
        if not names:
            rows.append((f"{tier} tier", "none needed", True, f"{t.provider}: {t.model}"))
            continue
        present = any(get(n) for n in names) or (t.provider == "codex" and codex_sign_in() is not None)
        rows.append((f"{tier} tier", " or ".join(names), present, f"{t.provider}: {t.model}"))
    def agent_row(what: str, agent: str, model: str) -> None:
        if agent not in AGENT_KEYS:
            rows.append((what, "not an agent Harbor-Master runs (claude-code or codex)", False, f"{agent}: {model}"))
        else:
            rows.append((what, " or ".join(needs(agent)), found(agent), f"{agent}: {model}"))

    for entry in cfg.difficulty.suite:
        agent_row("difficulty suite", entry.agent, entry.model)
    d = cfg.difficulty
    if d.cheap_solve and all(e.agent != d.cheap_agent for e in d.suite):      # the worker model's tries run in this agent
        agent_row("cheap solve", d.cheap_agent, cfg.tiers.worker.model)
    for name in OTHER_KEYS:
        rows.append(("task store", name, bool(get(name)), cfg.about.tasks or "none named in about.toml"))
    return rows


def scrub(text: str) -> str:
    """Replace every loaded key value and anything shaped like a key."""
    for name in {n for names in [*PROVIDER_KEYS.values(), *AGENT_KEYS.values()] for n in names} | set(OTHER_KEYS):
        value = None if name in PATH_KEYS else get(name)
        if value and len(value) >= 8:
            text = text.replace(value, f"[{name}]")
    return KEY_PATTERN.sub("[key]", text)
