"""A backend that makes no network calls and spends nothing.

Outcomes are drawn deterministically from each task's digest, so a run can be repeated.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import random

from ..core.config import Config, SuiteEntry
from ..tasks import Task
from .base import Backend, Decision, TaskDropped, TrialResult, Usage


def _rng(*parts: object) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))


def latent(task: Task) -> dict:
    """Hidden properties of a simulated task, fixed by its original digest."""
    r = _rng("latent", task.origin_digest)
    fixed = task.meta.setdefault("_fixed", [])
    return {
        "oracle_fails": r.random() < 0.08 and "oracle" not in fixed,
        "verifier_broken": r.random() < 0.14 and "verifier" not in fixed,
        "design_flaw": r.random() < 0.15 and "design" not in fixed,
        "hard": r.random() < 0.30,
        "fixable": r.random() < 0.65,
        "importance": r.random(),
        # drawn last, so that adding them left every earlier draw as it was
        "shortcut": r.random() < 0.05 and "verifier" not in fixed,       # the verifier accepts an empty output
        "unstated": r.random() < 0.07 and "design" not in fixed,         # the tests check what the instruction omits
    }


PACE = 2.0      # seconds one pretend agent session or container run takes, so that a person can watch each stage
TRIAL = 10.0    # and the least an agent's attempt at a task takes: the difficulty run is the long stage, as it is for real


class SimulatedBackend(Backend):
    """Stands in for all three tiers and for Harbor.

    Nothing here computes for long: an outcome is one draw from the task's digest. The only time a
    simulated run takes is the pause below, which exists so that the screen shows stages going by.
    Every agent session and every container run pauses once, for PACE. An agent's attempt at a task
    pauses for TRIAL or a little more, so the difficulty run is visibly the long stage. A decision does
    not pause. `speed` scales the pauses, and 0 removes them: a whole run then takes a fraction of a second.
    At the usual pace a task spends about a minute in its stages, most of it in the difficulty run.
    """

    def __init__(self, cfg: Config, usage: Usage, speed: float = 1.0):
        super().__init__(cfg, usage)
        self.speed = speed
        # Tasks whose first trial hangs, to show what happens then: HM_SIMULATE_HANG=t0007,t0002
        self.hangs = {n.strip() for n in os.environ.get("HM_SIMULATE_HANG", "").split(",") if n.strip()}

    async def _step(self, seconds: float = PACE) -> None:
        if self.speed > 0:
            await asyncio.sleep(seconds * self.speed)

    def _spend(self, tier: str, cost: float, tokens: int) -> None:
        self.usage(tier, cost, tokens)

    # ----- tier D: typed decisions with a probability -----

    async def decide(self, gate: str, choices: list[str], task: Task) -> Decision:
        r = _rng("decide", gate, task.digest)
        text = task.instruction.lower()
        if gate == "format":
            value = ("multiple_choice" if "multiple choice" in text or "choose one of" in text else
                     "question_answering" if "answer the question" in text else "agentic")
        elif gate == "verifier":
            value = ("none" if not task.has_tests else
                     "llm_judge" if "rubric" in text or "judged by" in text else "programmatic")
        elif gate == "verifier_cost":
            value = "costly" if "gpu" in text else "cheap"
        elif gate == "duplicate":
            value = "distinct"
        else:
            value = choices[0]
        tokens = max(200, len(task.instruction) // 4)
        cost = tokens * 0.042e-6
        self._spend("decision", cost, tokens)
        # Mostly sure, as the real decision model is, and unsure about one answer in twelve.
        prob = 0.70 + 0.14 * r.random() if r.random() < 0.08 else 0.90 + 0.099 * r.random()
        return Decision(value, prob, "decision", self.cfg.tiers.decision.model, cost, tokens)

    async def confirm(self, gate: str, choices: list[str], task: Task, first: Decision) -> Decision:
        """Tier W re-decides a gate that tier D was unsure about, and adds a rationale."""
        tokens = 1500 + len(task.instruction) // 4
        cost = 0.02
        self._spend("worker", cost, tokens)
        await self._step()
        return Decision(first.value, 0.97, "worker", self.cfg.tiers.worker.model, cost, tokens,
                        f"Read the task and confirmed {gate} = {first.value}.")

    async def importance(self, brief: str, task: Task) -> float | None:
        r = _rng("importance", task.digest)
        tokens = (len(task.instruction) + len(brief)) // 4 + 200
        self._spend("decision", tokens * 0.042e-6, tokens)
        return 4 * latent(task)["importance"] + r.uniform(-0.6, 0.6)       # a noisy first look

    async def compare(self, brief: str, a: Task, b: Task) -> float:
        """Probability that `a` matters more than `b` under the lead's brief, both orders averaged."""
        r = _rng("compare", a.digest, b.digest)
        gap = latent(a)["importance"] - latent(b)["importance"]
        tokens = (len(a.instruction) + len(b.instruction) + len(brief)) // 2
        self._spend("decision", 2 * tokens * 0.042e-6, 2 * tokens)
        return min(0.99, max(0.01, 1 / (1 + math.exp(-7 * gap)) + r.uniform(-0.08, 0.08)))

    # ----- tier W and F: agent sessions that end in one typed record -----

    async def adapt(self, benchmark: str, n_tasks: int) -> dict:
        self._spend("worker", 0.80, 60_000)
        await self._step()
        return {"adapter": "built", "cost": 0.80, "tokens": 60_000}

    async def write_mutants(self, task: Task, n: int = 8) -> dict:
        self._spend("worker", 0.30, 22_000)
        await self._step()
        return {"mutants": [{"id": f"m{i}", "kind": "wrong" if i < n // 2 else "equivalent"} for i in range(n)],
                "cost": 0.30, "tokens": 22_000}

    async def covers(self, task: Task, first: Decision | None = None) -> Decision:
        value = "unstated" if latent(task)["unstated"] else "covered"
        if first is not None:
            self._spend("worker", 0.05, 6_000)
            await self._step()
            return Decision(value, 0.97, "worker", self.cfg.tiers.worker.model, 0.05, 6_000,
                            "Read the tests against the instruction.")
        r = _rng("covers", task.digest)
        tokens = max(400, len(task.instruction) // 2)
        cost = tokens * 0.042e-6
        self._spend("decision", cost, tokens)
        return Decision(value, 0.70 + 0.14 * r.random() if r.random() < 0.08 else 0.90 + 0.099 * r.random(),
                        "decision", self.cfg.tiers.decision.model, cost, tokens)

    async def diagnose(self, task: Task, defect: str, why: str) -> Decision:
        x = _rng("diagnose", task.digest, defect).random()
        value = "drop" if x < 0.05 else "write_solution" if defect == "solution" else "repair_solution"
        self._spend("decision", 0.002, 1_500)
        await self._step()
        return Decision(value, 0.9, "decision", self.cfg.tiers.decision.model, 0.002, 1_500,
                        {"drop": "Its grading needs data the task does not ship.", "write_solution": "Its instruction and data are "
                         "enough to write one.", "repair_solution": "The reference solution reads a path the image does not have."}[value])

    async def audit(self, task: Task, round_no: int) -> Decision:
        r = _rng("audit", task.digest, round_no)
        flaw = latent(task)["design_flaw"]
        x = r.random()
        value = ("fix" if x < 0.75 else "drop") if flaw else ("accept" if x < 0.85 else "unsure")
        self._spend("worker", 0.40, 30_000)
        await self._step()
        notes = {"accept": "Tests trace to the instruction. Failures come from the science, not from formatting.",
                 "fix": "The verifier asserts a tolerance the instruction never states.",
                 "drop": "The reference output was produced by a flawed method. The task cannot be repaired.",
                 "unsure": "Two trajectories reach the right idea and fail on output schema. Evidence is mixed."}
        return Decision(value, 0.6 if value == "unsure" else 0.9, "worker", self.cfg.tiers.worker.model,
                        0.40, 30_000, notes[value])

    async def second_opinion(self, task: Task, round_no: int) -> Decision:
        r = _rng("frontier", task.digest, round_no)
        value = "accept" if r.random() < 0.6 else "fix"
        self._spend("frontier", 1.50, 45_000)
        await self._step()
        return Decision(value, 0.93, "frontier", self.cfg.tiers.frontier.model, 1.50, 45_000,
                        "Read four trajectories and the verifier. " +
                        ("The failures are legitimate." if value == "accept" else "The schema check is unstated."))

    async def final_audit(self, task: Task) -> Decision:
        """How the frontier agents failed, after the difficulty run: the agents' fault, so the task is hard, or the task's."""
        x = _rng("final-audit", task.digest).random()
        value = "accept" if x < 0.82 else "broken" if x < 0.94 else "unsure"
        self._spend("worker", 0.40, 30_000)
        await self._step()
        notes = {"accept": "The frontier agents reached the setup and failed on the science itself: wrong method, or an answer out of "
                           "tolerance. The task is hard, not broken.",
                 "broken": "Every failing frontier trial stopped at the same point: a package the task needs is missing from its image, "
                           "so the agents never got to the problem. That is the environment, not difficulty.",
                 "unsure": "Some trials failed on the science and some on the output's format. The evidence is mixed."}
        return Decision(value, 0.6 if value == "unsure" else 0.9, "worker", self.cfg.tiers.worker.model, 0.40, 30_000, notes[value])

    async def repair(self, task: Task, defect: str, round_no: int, tier: str, why: str = "") -> dict:
        ok = latent(task)["fixable"] or tier == "frontier"
        cost, tokens = (1.50, 50_000) if tier == "frontier" else (0.50, 35_000)
        self._spend(tier, cost, tokens)
        await self._step()
        return {"fixed": ok, "cost": cost, "tokens": tokens,
                "what": {"solution": "wrote solution/solve.sh from the instruction and the environment",
                         "oracle": "pinned a dependency the reference solution needs",
                         "verifier": "the verifier now rejects wrong-unit output",
                         "design": "stated the tolerance in the instruction"}.get(defect, defect)}

    async def write_report(self, task: Task, blackboard: dict) -> dict:
        self._spend("worker", 0.20, 12_000)
        await self._step()
        return {"cost": 0.20, "tokens": 12_000}

    async def answer(self, question: str, context: str) -> str:
        """The chat tier. There is no model here: the caller picked the records that fit the question."""
        self._spend(self.cfg.tiers.chat, 0.01, 1_500)
        await self._step()
        return context

    # ----- Harbor: runs of the verifier and of agents -----

    async def judge_stuck(self, what: str, log: str, quiet: float) -> Decision:
        self._spend("worker", 0.03, 6_000)
        await self._step()
        return Decision("hung", 0.9, "worker", self.cfg.tiers.worker.model, 0.03, 6_000,
                        "The container has been waiting on a package download that stopped answering; nothing has "
                        "been written since, and nothing will move on its own.")

    async def run_oracle(self, task: Task) -> TrialResult:
        await self._step()
        if task.name in self.hangs:         # it hangs once: the person retries it, or drops the task
            self.hangs.discard(task.name)
            quiet = self.cfg.run.quiet_minutes * 60
            if await self.stuck(task, "the reference solution's run", "(simulated: the log stopped)", quiet) == "drop":
                raise TaskDropped("hung", f"a trial was stuck for {quiet / 60:.0f} minutes, and was dropped")
            await self._step()
        return TrialResult(not latent(task)["oracle_fails"], 1.0, 0, 0, 0.0, 40.0)

    async def run_noop(self, task: Task) -> TrialResult:
        await self._step()
        return TrialResult(False, 0.0, 0, 0, 0.0, 20.0)

    async def run_mutant(self, task: Task, mutant: dict, stage: str = "probe") -> bool:
        """True when the verifier accepted the mutant."""
        await self._step()
        if mutant["id"].startswith("trivial"):
            return latent(task)["shortcut"] and mutant["id"] == "trivial-empty"
        if mutant["kind"] == "equivalent":
            return True
        return latent(task)["verifier_broken"] and mutant["id"] == "m1"

    async def run_trial(self, task: Task, entry: SuiteEntry, index: int, max_hours: float = 0) -> TrialResult:
        r = _rng("trial", task.digest, entry.agent, entry.model, index)
        p = 0.08 if latent(task)["hard"] else 0.85
        cheap = all(entry.model != e.model for e in self.cfg.difficulty.suite)     # a smaller model: the cheap first tries
        if cheap:
            p = 0.02 if latent(task)["hard"] else 0.6
        passed = r.random() < p
        tokens_in, tokens_out = r.randint(150_000, 900_000), r.randint(8_000, 40_000)
        seconds = r.uniform(240, 1500)
        await self._step(TRIAL + r.uniform(0.0, 4.0))
        price_in, price_out = (0.4e-6, 2e-6) if cheap else (2.0e-6, 10e-6)
        return TrialResult(passed, float(passed), tokens_in, tokens_out,
                           round(tokens_in * price_in + tokens_out * price_out, 2), seconds,
                           agent_version=entry.version)      # a simulation runs the version it was asked for
