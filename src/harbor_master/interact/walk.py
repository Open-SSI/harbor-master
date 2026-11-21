"""What each stage does, in a line or two: shown as a simulated run reaches it, so that the run reads as a walk-through."""
from __future__ import annotations

STAGE = {
    "triage": "Every task is screened for cents. Is it work an agent executes, with a reference solution and a program that grades "
              "it? Is the answer out of the agent's reach? A task that fails a gate stops here.",
    "rank": "Every task that passed is scored for how much it matters under your brief, and the ones near the top are compared in pairs.",
    "select": "The top tasks go on to the costly stages. You approve the list before any trial is spent.",
    "verify": "The task's own reference solution must pass. A run that does nothing must fail, and so must an empty or placeholder output.",
    "probe": "The verifier is tested: wrong variants of the solution must be rejected, right ones accepted. "
             "Then the instruction must state everything the tests check.",
    "worker": "The worker agent tries the task twice. A task it solves both times is too easy, and no frontier trial is spent on it.",
    "audit": "An agent reads how the worker agent failed. Was the task hard, or was it broken?",
    "fix": "A broken task is repaired, and the new version goes back through verify, probe, the worker's tries and the audit.",
    "difficulty": "Last, once per task, and only for a task that passed every cheap check: two frontier agents try three times "
                  "each, and the task is kept if at most 3 of the 6 pass. Nothing loops back from here.",
    "report": "The frontier agents' failures are audited, and one page per kept task goes to a reviewer, with a recommendation: "
              "reject when the failures look like the task's fault, such as a broken environment. You type accept or reject under it.",
}
