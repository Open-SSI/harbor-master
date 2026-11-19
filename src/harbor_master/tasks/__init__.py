"""Tasks and benchmarks: the things being curated."""
from .intake import detect, find_tasks, load_benchmark, load_task
from .model import Task, digest_dir
from .registry import REGISTRY, read_registry, register, resolve
from . import store, supported

__all__ = ["REGISTRY", "Task", "detect", "digest_dir", "load_benchmark", "load_task", "read_registry", "register"]
