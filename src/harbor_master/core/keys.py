"""API keys: environment first, then secrets.env in the repository folder, git-ignored (DESIGN.md Section 8)."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from .config import Config
from .identity import config_dir

# The variable each provider reads. Values never enter hm.toml, the ledger, or an upload.
# Each agent takes an API key, or a sign-in to a subscription. The sign-in is used when both are set.
AGENT_KEYS = {  # the agents that also take a sign-in to a subscription, which Harbor does not list: [API key, sign-in]
    "claude-code": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"],
    "codex": ["OPENAI_API_KEY", "CODEX_AUTH_JSON_PATH"],
}
REFERENCE = re.compile(r"\$\{(\w+)\}")      # ${NAME} in a configuration's env: a key named, never written out
PATH_KEYS = {"CODEX_AUTH_JSON_PATH"}     # the value is the path of a file that holds the credential
PROVIDER_KEYS = {
    "openrouter": ["OPENROUTER_API_KEY"],
    "claude-agent-sdk": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"],  # either one
    "codex": ["CODEX_AUTH_JSON_PATH"],      # the sign-in that `codex login` writes. ~/.codex/auth.json is found without it
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


@lru_cache(maxsize=None)
def harbor_agents() -> dict[str, list[str]]:
    """Every agent the installed Harbor has, with the variables it reads its API key from. Harbor is the one
    list: an agent it gains is usable here with no change. Empty when Harbor cannot be imported."""
    try:
        from harbor.agents.factory import AgentFactory
        from harbor.models.agent.name import AgentName
    except Exception:           # not installed, or a version without these modules
        return {}
    out: dict[str, list[str]] = {}
    for name in sorted(n.value for n in AgentName):
        names: list[str] = []
        try:
            access = getattr(getattr(AgentFactory.get_agent_class(AgentName(name)), "MODEL_CONNECTION", None), "agent_provider_access", None)
            names = list(getattr(access, "api_key_candidate_names", None) or [])
        except Exception:       # an agent whose own package is not installed: it is still an agent by name
            pass
        out[name] = names
    return out


def known_agent(agent: str) -> bool:
    """Whether Harbor has this agent. When Harbor cannot be asked, nothing is called unknown."""
    listed = harbor_agents()
    return not listed or agent in listed


def needs(agent: str, model: str = "", key: str = "") -> list[str]:
    """The variables that can hold this configuration's credential: any one of them is enough.

    The key the configuration names comes first. Then the agent's sign-in, where it has one, and the
    variables Harbor says the agent reads. An agent that calls models through LiteLLM reads the key of
    the provider its model names: `openrouter/...` reads OPENROUTER_API_KEY.
    """
    names = [key] if key else []
    if not key or key in AGENT_KEYS.get(agent, []):     # a key for another provider's endpoint is not replaced by the sign-in
        names += AGENT_KEYS.get(agent, [])
    if not key:
        names += harbor_agents().get(agent, [])
        if "/" in model:
            names.append(model.split("/", 1)[0].upper().replace("-", "_") + "_API_KEY")
    return list(dict.fromkeys(n for n in names if n))


def found(key: str, agent: str = "", model: str = "") -> bool:
    """Whether a configuration can run: its key, or the agent's sign-in in its place."""
    return any(get(n) for n in needs(agent, model, key))


def agent_env(agent: str = "", model: str = "", key: str = "", settings: dict[str, str] | None = None) -> dict[str, str]:
    """The credentials an agent trial gets, as environment variables: the ones its configuration can
    use, and any that its own settings name as ${NAME}. No other key in secrets.env is passed on.

    Where an agent has both an API key and a sign-in, the sign-in is used, as for the curation agents.
    Harbor already prefers a Codex sign-in file, and prefers the Claude key unless told otherwise.
    """
    wanted = ["OPENROUTER_API_KEY", *sum(AGENT_KEYS.values(), []), *needs(agent, model, key),
              *(m for v in (settings or {}).values() for m in REFERENCE.findall(str(v)))]
    env = {n: v for n in dict.fromkeys(wanted) if (v := get(n))}
    if "CLAUDE_CODE_OAUTH_TOKEN" in env and agent in ("", "claude-code") and key in ("", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
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
        if t.provider == "codex" and not present:        # Codex's own sign-in file needs no variable
            present = (Path.home() / ".codex" / "auth.json").is_file()
        rows.append((f"{tier} tier", " or ".join(names), present, f"{t.provider}: {t.model}"))
    def agent_row(what: str, agent: str, model: str, key: str = "") -> None:
        names = needs(agent, model, key)
        if not known_agent(agent):
            rows.append((what, "not an agent Harbor has", False, f"{agent}: {model}"))
        else:
            rows.append((what, " or ".join(names) or "none named", any(get(n) for n in names), f"{agent}: {model}"))

    for entry in cfg.difficulty.suite:
        agent_row("difficulty suite", entry.agent, entry.model, entry.key)
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
