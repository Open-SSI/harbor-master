import random
from pathlib import Path

import pytest
import tomli_w

from harbor_master import ledger, tasks
from harbor_master.core import config as config_mod
from harbor_master.core import identity


@pytest.fixture
def ident(tmp_path, monkeypatch):
    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / "config"))
    return identity.create("Test Runner", "Test University", "t@example.org", "test-runner")


def make_benchmark(root: Path, name: str, count: int, seed: int = 0) -> Path:
    rng = random.Random(f"{name}-{seed}")
    for i in range(count):
        d = root / name / f"t{i:04d}"
        (d / "tests").mkdir(parents=True)
        (d / "solution").mkdir()
        area = rng.choice(["docking", "spectra", "retrosynthesis"])
        (d / "instruction.md").write_text(f"Task {i} on {area}. Write and run code to produce results.csv.\n")
        (d / "task.toml").write_text(tomli_w.dumps({"task": {"name": f"{name}/t{i:04d}"}, "metadata": {"field": area}}))
        (d / "tests" / "test.sh").write_text(f"# verifier {i}\n")
        (d / "solution" / "solve.sh").write_text(f"# solution {i}\n")
    return root / name


def write_index(ws: Path, cfg: config_mod.Config, people: list[dict]) -> None:
    """A small index in `ws`: its protocol and its contributor list."""
    ws.mkdir(parents=True, exist_ok=True)
    (ws / config_mod.PROTOCOL_NAME).write_text(tomli_w.dumps({k: getattr(cfg, k).model_dump() for k in config_mod.PROTOCOL_SECTIONS}))
    (ws / ledger.CONTRIBUTORS).write_text(tomli_w.dumps({"contributor": people}))
    (ws / ".gitignore").write_text(".hm/\n")


@pytest.fixture
def workspace(tmp_path, ident):
    ws = tmp_path / "ws"
    cfg = config_mod.Config()
    cfg.select.require_brief = False
    cfg.select.require_signoff = False
    cfg.select.top_k, cfg.select.max_fraction = 20, 1.0      # 20 of the 24 test tasks go on
    write_index(ws, cfg, [{"name": ident.name, "affiliation": ident.affiliation, "github": ident.github,
                           "key": ident.public_key}])
    path = make_benchmark(tmp_path / "fake", "demo-bench", 24)
    tasks.register(ws, "demo-bench", path)
    return ws


@pytest.fixture
def session(workspace, ident):
    from harbor_master.engine import Session
    cfg = config_mod.load(workspace)
    return Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=True, as_real=True, speed=0, no_input=True)
