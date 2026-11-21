"""The cheap checks on a whole benchmark: a failed one rejects it, and says why; a warning changes nothing."""
from harbor_master.core import config as config_mod
from harbor_master.tasks import checks

from conftest import make_benchmark


def test_a_fit_benchmark_passes_and_an_unfit_one_fails_with_why(tmp_path):
    source = make_benchmark(tmp_path, "good-bench", 12)
    fit = {"id": "good-bench", "released": "2026-01-02", "license": "MIT", "task_kind": "agentic", "best_reported": 0.2}
    assert not any(c.failed for c in checks.run(fit, source, config_mod.Config(), simulate=True))
    unfit = checks.run({"id": "old", "released": "2021-05-01", "task_kind": "multiple_choice", "access": "held_out"},
                       None, config_mod.Config(), simulate=True)
    assert {c.key for c in unfit if c.failed} == {"recency", "kind", "access"}
    assert {c.key for c in checks.run({"id": "x", "best_reported": 0.97, "license": ""}, None, config_mod.Config(), True)
            if c.status == "warn"} == {"solved", "license"}                  # how far solved, and the license, only warn
    assert checks.why(unfit).startswith("Released 2021-05-01")
