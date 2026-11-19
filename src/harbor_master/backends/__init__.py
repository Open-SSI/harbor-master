"""Backends: who actually makes the decisions and runs the trials."""
from ..core.config import Config
from .base import Backend, Decision, NotImplementedBackend, TrialResult, Usage
from .simulated import SimulatedBackend

__all__ = ["Backend", "Decision", "NotImplementedBackend", "SimulatedBackend", "TrialResult", "Usage", "make_backend"]


def make_backend(cfg: Config, usage: Usage, simulate: bool, speed: float = 1.0, run_dir=None,
                 assume_after: str = "") -> Backend:
    if simulate:
        return SimulatedBackend(cfg, usage, speed)
    from .real import RealBackend      # imported here so that a simulated run needs no agent SDK
    return RealBackend(cfg, usage, run_dir, assume_after)
