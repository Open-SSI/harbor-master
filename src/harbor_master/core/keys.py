"""API keys: environment first, then secrets.env in the repository folder, git-ignored (DESIGN.md Section 8)."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .config import Config
from .identity import config_dir

# The variable each provider reads. Values never enter hm.toml, the ledger, or an upload.
# Each agent takes an API key, or a sign-in to a subscription. The sign-in is used when both are set.
AGENT_KEYS = {  # the agents a difficulty suite can name: [API key, sign-in]
    "claude-code": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"],
    "codex": ["OPENAI_API_KEY", "CODEX_AUTH_JSON_PATH"],
}
PATH_KEYS = {"CODEX_AUTH_JSON_PATH"}     # the value is the path of a file that holds the credential
PROVIDER_KEYS = {
    "openrouter": ["OPENROUTER_API_KEY"],
    "claude-agent-sdk": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"],  # either one
    "mock": [],
}
OTHER_KEYS = ["HF_TOKEN"]
KEY_PATTERN = re.compile(r"(sk-[A-Za-z0-9_\-]{20,}|hf_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}"
                         r"|eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,})")     # the last is a sign-in token


SECRETS = "secrets.env"


def project_root() -> Path | None:
    """The Harbor-Master repository folder: the nearest folder upward that has secrets.env.example,
    or the checkout this code runs from."""
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / "secrets.env.example").exists():
            return d
    source = Path(__file__).resolve().parents[3]
    return source if (source / "secrets.env.example").exists() else None


def secrets_path() -> Path:
    """Where the keys file is. In order: $HM_SECRETS, a secrets.env found from here upward, the
    repository folder. The file is git-ignored there. With HM_CONFIG_DIR set (tests), it stays in that folder."""
    if os.environ.get("HM_SECRETS"):
        return Path(os.environ["HM_SECRETS"])
    if os.environ.get("HM_CONFIG_DIR"):
        return config_dir() / SECRETS
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / SECRETS).exists():
            return d / SECRETS
    root = project_root()
    return (root or config_dir()) / SECRETS


def ensure_ignored(path: Path) -> None:
    """Make sure git will never pick the keys file up: add it to the folder's .gitignore if it is missing."""
    ignore = path.parent / ".gitignore"
    lines = ignore.read_text().splitlines() if ignore.exists() else []
    if path.name not in [line.strip() for line in lines]:
        ignore.write_text("\n".join([*lines, path.name]) + "\n")


def _file_values() -> dict[str, str]:
    path = secrets_path()
    out: dict[str, str] = {}
    if path.exists():
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


def found(key: str, agent: str = "") -> bool:
    """Whether a configuration that names `key` can run: the key itself, or the agent's sign-in in its place."""
    names = AGENT_KEYS.get(agent, [])
    return bool(get(key)) or (key in names and any(get(n) for n in names))


def agent_env() -> dict[str, str]:
    """The credentials an agent trial gets, as environment variables.

    Where an agent has both an API key and a sign-in, the sign-in is used, as for the curation agents.
    Harbor already prefers a Codex sign-in file, and prefers the Claude key unless told otherwise.
    """
    env = {n: v for n in ("OPENROUTER_API_KEY", *sum(AGENT_KEYS.values(), [])) if (v := get(n))}
    if "CLAUDE_CODE_OAUTH_TOKEN" in env:
        env["CLAUDE_FORCE_OAUTH"] = "1"
    return env


def set_key(name: str, value: str) -> None:
    path = secrets_path()
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
        present = any(get(n) for n in names)
        rows.append((f"{tier} tier", " or ".join(names), present, f"{t.provider}: {t.model}"))
    for entry in cfg.difficulty.suite:
        names = AGENT_KEYS.get(entry.agent)
        if names is None:
            rows.append(("difficulty suite", "unknown agent", False, f"{entry.agent}: {entry.model}"))
        else:
            rows.append(("difficulty suite", " or ".join(names), any(get(n) for n in names), f"{entry.agent}: {entry.model}"))
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
