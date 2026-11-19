"""The real backend: Claude for the model tiers, Harbor for every run.

The decision tier runs on Jev through OpenRouter (provider "openrouter"), and on nothing else: Jev
returns real probabilities and answers all of a task's gates in one request. The worker and
frontier tiers run on Claude, or on OpenAI models through Codex.

Built so far: the admission gates, pairwise comparison, mutant writing, answers, and Harbor
runs of the reference solution, a no-op, a mutant, and an agent. Not built: the adapter
builder, the audit, the second opinion, the repair, and the report writer. Those raise.
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path

from .. import skills
from ..core.config import Config, SuiteEntry
from ..curation import gates
from ..tasks import Task
from . import claude, harbor, jev, sessions
from .base import Backend, Decision, NotImplementedBackend, TrialResult, Usage
from .simulated import SimulatedBackend

CLAUDE_PROVIDERS = sessions.PROVIDERS       # what an agent session can run on: Claude, or OpenAI models through Codex


def describe(task: Task, limit: int = 6000) -> str:
    """What a model is shown about a task: its instruction, its metadata, its files. Never the solution."""
    files = sorted(str(p.relative_to(task.path)) for p in task.path.rglob("*")
                   if p.is_file() and "solution" not in p.relative_to(task.path).parts and ".git" not in p.parts)
    meta = {**task.meta.get("task", {}), **task.meta.get("metadata", {})}
    for drop in ("authors", "author_email", "author_name", "author_profile", "_benchmark_license", "_fixed"):
        meta.pop(drop, None)
    text = task.instruction if len(task.instruction) <= limit else task.instruction[:limit] + "\n[instruction cut here]"
    license_file = next((p for p in (task.path / "LICENSE.md", task.path / "LICENSE") if p.exists()), None)
    license_text = license_file.read_text(errors="replace")[:800] if license_file else "(no license file in the task)"
    if task.meta.get("_benchmark_license"):
        license_text = (f"The benchmark this task belongs to is released under {task.meta['_benchmark_license']}.\n"
                        f"The task's own license file:\n{license_text}")
    return (f"TASK {task.task_id}\n\nINSTRUCTION\n{text}\n\nMETADATA\n{json.dumps(meta, indent=1, default=str)[:2000]}\n\n"
            f"VERIFIER SETTINGS\n{json.dumps(task.meta.get('verifier', {}), default=str)}\n\n"
            f"LICENSE\n{license_text}\n\n"
            f"FILES ({len(files)}, solution left out)\n" + "\n".join(files[:80]) +
            f"\n\nHas a reference solution: {task.has_solution}. Has tests: {task.has_tests}.")


def card(task: Task, limit: int = 1200) -> str:
    """A short description of a task, for judging how much it matters. Ranking needs what the task is
    about, not its files or its verifier, so this is a fraction of the size of `describe`."""
    info, md = task.meta.get("task", {}), task.meta.get("metadata", {})
    lines = [f"Task: {task.name}"]
    if info.get("description"):
        lines.append(f"Summary: {info['description']}")
    where = " / ".join(str(md[k]) for k in ("domain", "field", "subfield") if md.get(k))
    if where:
        lines.append(f"Field: {where}")
    tags = [*info.get("keywords", []), *md.get("tags", [])]
    if tags:
        lines.append("Keywords: " + ", ".join(dict.fromkeys(map(str, tags))))
    text = " ".join(re.sub(r"<!--.*?-->", " ", task.instruction, flags=re.S).split())     # no canary comments
    lines.append("Instruction, beginning: " + (text[:limit] + ("..." if len(text) > limit else "")))
    return "\n".join(lines)


def _choice_schema(choices: list[str], rationale: bool = False) -> dict:
    props = {"value": {"type": "string", "enum": choices},
             "confidence": {"type": "number", "description": "Probability from 0 to 1 that the value is right."}}
    if rationale:
        props["rationale"] = {"type": "string", "description": "One or two sentences citing the task's own text or files."}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


MUTANT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["mutants"],
    "properties": {"mutants": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "kind", "what", "files"],
        "properties": {
            "id": {"type": "string"},
            "kind": {"type": "string", "enum": ["wrong", "equivalent"]},
            "what": {"type": "string", "description": "One sentence: what was changed and why the verifier should reject or accept it."},
            "files": {"type": "array", "description": "Complete replacement files, with paths relative to solution/.",
                      "items": {"type": "object", "additionalProperties": False, "required": ["path", "content"],
                                "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}}}}}},
}


def tests_text(task: Task, limit: int = 14000) -> str:
    """The tests' own text, for the question of whether the instruction states what they check."""
    folder, out, left = task.path / "tests", [], limit
    for f in (sorted(p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts) if folder.is_dir() else ()):
        if left <= 0 or f.stat().st_size > 200_000:
            continue
        try:
            text = f.read_text()
        except UnicodeDecodeError:
            continue                # data, not a test
        out.append(f"--- {f.relative_to(folder)}\n{text[:left]}")
        left -= len(text)
    return "\n".join(out) or "(no readable tests)"


COVERAGE = ["covered", "unstated"]
REPORTED_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["source", "tasks"],
    "properties": {
        "source": {"type": "string", "description": "The files the results were read from, or why there are none."},
        "tasks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["task", "pass_rate", "model"],
            "properties": {"task": {"type": "string", "description": "The task's name, exactly as listed."},
                           "pass_rate": {"type": "number", "description": "From 0 to 1."},
                           "model": {"type": "string", "description": "The model or agent that reached it."}}}}},
}
LATER = ["verify", "probe", "difficulty", "audit"]


class RealBackend(Backend):
    """`assume_after` names the last stage that runs for real. Every later stage is assumed:
    its results come from the simulated backend, cost nothing, and are marked in the ledger."""

    def __init__(self, cfg: Config, usage: Usage, run_dir: Path, assume_after: str = ""):
        super().__init__(cfg, usage)
        self.run_dir, self.jobs, self.scratch = run_dir, run_dir / "harbor", run_dir / "scratch"
        self.assumed = set(LATER[LATER.index(assume_after) + 1:]) if assume_after else set()
        self.sim = SimulatedBackend(cfg, lambda tier, cost, tokens: None, speed=0)
        if cfg.tiers.decision.provider != "openrouter":     # a general model states a confidence, not a probability
            raise NotImplementedBackend(
                f"The decision tier is set to provider {cfg.tiers.decision.provider!r}. It runs on Jev only: "
                "provider \"openrouter\", model \"typesafe/jev-1.14\".")
        for tier in ("worker", "frontier"):
            provider = cfg.tier(tier).provider
            if provider not in CLAUDE_PROVIDERS:
                raise NotImplementedBackend(
                    f"The {tier} tier is set to provider {provider!r}, which is not built. The worker and frontier "
                    "tiers take \"claude-agent-sdk\" or \"codex\".")

    async def _ask(self, tier: str, system: str, prompt: str, schema: dict, cwd: Path | None = None,
                   max_turns: int = 6) -> claude.Reply:
        """One agent session on a tier, counted under it."""
        name = self.cfg.tiers.chat if tier == "chat" else tier
        reply = await sessions.ask(self.cfg.tier(name), system, prompt, schema, cwd=cwd, max_turns=max_turns)
        self.usage(name, reply.cost, reply.tokens)
        return reply

    def _state(self, task: Task) -> dict:
        """What Jev is shown. Kept under its 32,000-byte fast path."""
        return {"task": describe(task, 6000)[:24000]}

    def _jev_decision(self, choices: list[str], answer: dict, cost: float, tokens: int, tag: str) -> Decision:
        if answer.get("type") != "choice" or answer.get("choice") not in choices:
            return Decision("", 0.0, "decision", self.cfg.tiers.decision.model, cost, tokens, skill=tag)   # declined: goes up a tier
        prob = float((answer.get("probabilities") or {}).get(answer["choice"], answer.get("confidence") or 0.0))
        return Decision(answer["choice"], prob, "decision", self.cfg.tiers.decision.model, cost, tokens, skill=tag)

    async def decide_gates(self, task: Task, wanted: dict[str, list[str]]) -> dict[str, Decision]:
        if not wanted:
            return {}
        questions, criteria, tag = gates.wording()
        asked = {g: jev.choice(questions[g], {c: criteria[g][c] for c in choices}) for g, choices in wanted.items()}
        answers, cost, tokens = await jev.decisions(self.cfg.tiers.decision.model, self._state(task), asked)
        self.usage("decision", cost, tokens)          # one request answers every gate
        share = cost / len(wanted)
        return {g: self._jev_decision(choices, answers.get(g) or {}, share, tokens // len(wanted), tag)
                for g, choices in wanted.items()}

    async def classify(self, state, questions: dict) -> dict:
        """Any typed questions about any state, for the session prompt. Decision tier on Jev only."""
        answers, cost, tokens = await jev.decisions(self.cfg.tiers.decision.model, state, questions)
        self.usage("decision", cost, tokens)
        return answers

    # ----- tier D -----

    async def decide(self, gate: str, choices: list[str], task: Task) -> Decision:
        return (await self.decide_gates(task, {gate: choices}))[gate]

    async def confirm(self, gate: str, choices: list[str], task: Task, first: Decision) -> Decision:
        skill, (questions, _, _) = skills.load("triage/confirm"), gates.wording()
        r = await self._ask("worker", skill.system,
                            skill.fill(question=questions[gate], first_value=first.value or "(no answer)",
                                       first_prob=f"{first.prob:.2f}", task=describe(task)),
                            _choice_schema(choices, rationale=True), cwd=task.path, max_turns=12)
        return Decision(r.data["value"], float(r.data["confidence"]), "worker", self.cfg.tiers.worker.model,
                        r.cost, r.tokens, r.data.get("rationale", ""), skill=skill.tag)

    async def importance(self, brief: str, task: Task) -> float | None:
        skill = skills.load("rank/score")
        state = {"brief_from_the_field_lead": brief or "(none given)", "task": card(task)}
        q = {"importance": {"type": "score", "instructions": skill.data["question"], "criteria": skill.data["levels"]}}
        answers, cost, tokens = await jev.decisions(self.cfg.tiers.decision.model, state, q)
        self.usage("decision", cost, tokens)
        answer = answers.get("importance") or {}
        return float(answer["score"]) if answer.get("type") == "score" and "score" in answer else None

    async def compare(self, brief: str, a: Task, b: Task) -> float:
        skill = skills.load("rank/compare")

        async def ask(first: Task, second: Task) -> float:
            state = {"brief_from_the_field_lead": brief or "(none given)",
                     "first_task": card(first), "second_task": card(second)}
            q = {"first_matters_more": jev.noul(skill.data["question"], skill.data["yes"], skill.data["no"])}
            answers, cost, tokens = await jev.decisions(self.cfg.tiers.decision.model, state, q)
            self.usage("decision", cost, tokens)
            return min(0.99, max(0.01, float((answers.get("first_matters_more") or {}).get("noul", 0.5))))

        return (await ask(a, b) + (1 - await ask(b, a))) / 2      # both orders, to cancel position bias

    # ----- tier W -----

    async def write_mutants(self, task: Task, n: int = 4) -> dict:
        if "probe" in self.assumed:
            return {**await self.sim.write_mutants(task), "cost": 0.0}
        skill = skills.load("probe/write-mutants")
        r = await self._ask("worker", skill.system, skill.fill(n=n, task=describe(task, 3000)), MUTANT_SCHEMA,
                            cwd=task.path, max_turns=40)
        mutants = [{**m, "files": {f["path"]: f["content"] for f in m["files"]}} for m in r.data["mutants"]]
        return {"mutants": mutants, "cost": r.cost, "tokens": r.tokens, "skill": skill.tag}

    async def reported(self, benchmark: str, repo: Path, names: list[str]) -> dict:
        skill = skills.load("triage/reported-results")
        listed = "\n".join(names[:1500]) + (f"\n(and {len(names) - 1500} more: they are named like these)" if len(names) > 1500 else "")
        r = await self._ask("worker", skill.system, skill.fill(benchmark=benchmark, n=len(names), names=listed),
                            REPORTED_SCHEMA, cwd=repo, max_turns=40)
        tasks = {t["task"]: {"pass_rate": t["pass_rate"], "model": t.get("model", "")} for t in r.data.get("tasks", [])}
        return {"tasks": tasks, "source": r.data.get("source", ""), "cost": r.cost, "tokens": r.tokens, "skill": skill.tag}

    async def covers(self, task: Task, first: Decision | None = None) -> Decision:
        if "probe" in self.assumed:
            d = await self.sim.covers(task, first)
            d.cost = 0.0
            return d
        skill = skills.load("probe/coverage")
        asked, tests = skill.data["coverage"], tests_text(task)
        if first is None:
            state = {"task": describe(task, 6000)[:12000], "tests": tests}
            answers, cost, tokens = await jev.decisions(self.cfg.tiers.decision.model, state,
                                                        {"coverage": jev.choice(asked["question"], asked["criteria"])})
            self.usage("decision", cost, tokens)
            return self._jev_decision(COVERAGE, answers.get("coverage") or {}, cost, tokens, skill.tag)
        said = (f"A first check said \"{first.value or '(no answer)'}\" with probability {first.prob:.2f}. "
                "Read the task's files where that helps.\n\n")
        r = await self._ask("worker", skill.data["system"], f"{asked['question']}\n\n{said}{describe(task)}\n\nTESTS\n{tests}",
                            _choice_schema(COVERAGE, rationale=True), cwd=task.path, max_turns=12)
        return Decision(r.data["value"], float(r.data["confidence"]), "worker", self.cfg.tiers.worker.model, r.cost, r.tokens,
                        r.data.get("rationale", ""), skill=skill.tag)

    async def answer(self, question: str, context: str) -> str:
        skill = skills.load("session/answer")
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
                  "additionalProperties": False}
        r = await self._ask("chat", skill.system, skill.fill(question=question, records=context), schema)
        return r.data["answer"]

    async def adapt(self, benchmark: str, n_tasks: int) -> dict:
        raise NotImplementedBackend("Building an adapter for input that is not in Harbor format is not built yet.")

    async def audit(self, task: Task, round_no: int) -> Decision:
        if "audit" in self.assumed:
            d = await self.sim.audit(task, round_no)
            d.cost = 0.0
            return d
        raise NotImplementedBackend("The audit is not built yet. Stop a real run earlier with --until probe.")

    async def second_opinion(self, task: Task, round_no: int) -> Decision:
        if "audit" in self.assumed:
            d = await self.sim.second_opinion(task, round_no)
            d.cost = 0.0
            return d
        raise NotImplementedBackend("The frontier second opinion is not built yet.")

    async def repair(self, task: Task, defect: str, round_no: int, tier: str) -> dict:
        if self.assumed and defect != "oracle":      # a real verify failure is never repaired by assumption
            return {**await self.sim.repair(task, defect, round_no, tier), "cost": 0.0}
        raise NotImplementedBackend("Repair is not built yet. The defect is recorded, and the task is left as it is.")

    async def write_report(self, task: Task, blackboard: dict) -> dict:
        if "audit" in self.assumed:
            return {"cost": 0.0, "tokens": 0}
        raise NotImplementedBackend("The report writer is not built yet.")

    # ----- Harbor -----

    def _settle(self, result: TrialResult, keep: bool, label: str) -> TrialResult:
        return harbor.settle(result, keep, self.run_dir / "trials", re.sub(r"[^A-Za-z0-9._-]+", "-", label),
                             self.run_dir.parents[1])

    async def run_oracle(self, task: Task) -> TrialResult:
        r = await harbor.run(task.path, self.jobs, "oracle")
        return self._settle(r, keep=not r.passed or bool(r.error), label=f"{task.name}.oracle.{uuid.uuid4().hex[:6]}")

    async def run_noop(self, task: Task) -> TrialResult:
        r = await harbor.run(task.path, self.jobs, "nop")
        return self._settle(r, keep=r.passed or bool(r.error), label=f"{task.name}.noop.{uuid.uuid4().hex[:6]}")

    async def run_mutant(self, task: Task, mutant: dict, stage: str = "probe") -> bool:
        if stage in self.assumed:
            return await self.sim.run_mutant(task, mutant, stage)
        copy = harbor.with_solution(task.path, mutant["files"], self.scratch)
        try:
            result = await harbor.run(copy, self.jobs, "oracle")
        finally:
            shutil.rmtree(copy.parent, ignore_errors=True)
        mutant["reward"], mutant["error"] = result.reward, result.error
        finding = (mutant["kind"] == "wrong") == result.passed      # a wrong one accepted, or a right one rejected
        self._settle(result, keep=finding or bool(result.error), label=f"{task.name}.mutant-{mutant['id']}")
        return result.passed

    async def run_trial(self, task: Task, entry: SuiteEntry, index: int, max_hours: float = 0) -> TrialResult:
        if "difficulty" in self.assumed:
            result = await self.sim.run_trial(task, entry, index)
            result.cost, result.tokens_in, result.tokens_out, result.seconds = 0.0, 0, 0, 0.0
            return result
        r = await harbor.run(task.path, self.jobs, entry.agent, entry.model,
                             {"version": entry.version, "reasoning_effort": entry.reasoning_effort}, with_keys=True,
                             max_agent_seconds=(max_hours or self.cfg.difficulty.max_agent_hours) * 3600,
                             agent_env=entry.env or None, key=entry.key)
        return self._settle(r, keep=True, label=f"{task.name}.{entry.agent}.{entry.model}.{index}")
