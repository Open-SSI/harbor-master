"""The real backend: Claude for the model tiers, Harbor for every run.

The decision tier runs on Jev through OpenRouter (provider "openrouter"), and on nothing else: Jev
returns real probabilities and answers all of a task's gates in one request. The worker and
frontier tiers run on Claude, or on OpenAI models through Codex.

Built: the admission gates, pairwise comparison, mutant writing, answers, Harbor runs of the reference
solution, a no-op, a mutant, and an agent, the audit of the worker agent's trials and its frontier second
opinion, the repair, and the audit of the frontier trials. The adapter builder lives in adapter.py. The
report is written by code from the records, with no model.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import tarfile
import uuid
from pathlib import Path

from .. import skills
from ..core.config import Config, SuiteEntry
from ..curation import gates
from ..tasks import Task
from ..tasks.model import digest_dir
from . import agents, claude, harbor, jev, sessions
from .base import Backend, Decision, NotImplementedBackend, TrialResult, Usage
from .simulated import SimulatedBackend

CLAUDE_PROVIDERS = sessions.PROVIDERS       # what an agent session can run on: Claude, or OpenAI models through Codex


def describe(task: Task, limit: int = 6000) -> str:
    """What a model is shown about a task: its instruction, its metadata, its files. Never the solution."""
    files = sorted(str(p.relative_to(task.path)) for p in task.path.rglob("*")
                   if p.is_file() and "solution" not in p.relative_to(task.path).parts and ".git" not in p.parts)
    meta = {**task.meta.get("task", {}), **task.meta.get("metadata", {})}
    for drop in ("authors", "author_email", "author_name", "author_profile", "_fixed"):
        meta.pop(drop, None)
    text = task.instruction if len(task.instruction) <= limit else task.instruction[:limit] + "\n[instruction cut here]"
    return (f"TASK {task.task_id}\n\nINSTRUCTION\n{text}\n\nMETADATA\n{json.dumps(meta, indent=1, default=str)[:2000]}\n\n"
            f"VERIFIER SETTINGS\n{json.dumps(task.meta.get('verifier', {}), default=str)}\n\n"
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
DIAGNOSES = ["write_solution", "repair_solution", "repair_verifier", "repair_environment", "drop"]
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
AUDIT = ["accept", "fix", "drop", "unsure"]
FINAL = ["accept", "broken", "unsure"]
REPAIR_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["fixed", "what"],
                 "properties": {"fixed": {"type": "boolean"},
                                "what": {"type": "string", "description": "What was changed, in which files. Or why nothing could be."}}}
DEFECTS = {"solution": "The task has no reference solution. Write solution/solve.sh that solves the task as its instruction asks, in its "
                       "environment, so that its tests pass. Solve it as a person would: never copy expected values out of the tests.",
           "oracle": "The reference solution does not pass its own tests, a no-op run passes, the reference passed once and failed "
                     "once, or a run failed for an infrastructure reason such as an image that does not build.",
           "verifier": "The verifier accepts a wrong answer, or rejects a correct one.",
           "design": "The tests check something the instruction does not state, or the task fails for a reason other than its difficulty."}


class RealBackend(Backend):
    """`assume_after` names the last stage that runs for real. Every later stage is assumed:
    its results come from the simulated backend, cost nothing, and are marked in the ledger."""

    def __init__(self, cfg: Config, usage: Usage, run_dir: Path, assume_after: str = ""):
        super().__init__(cfg, usage)
        self.run_dir, self.jobs, self.scratch = run_dir, run_dir / "harbor", run_dir / "scratch"
        self.assumed = set(LATER[LATER.index(assume_after) + 1:]) if assume_after else set()
        self.sim = SimulatedBackend(cfg, lambda tier, cost, tokens: None, speed=0)
        self.kept: dict[str, list[dict]] = {}       # each task version's agent trials, by digest: what the audits read
        self.mutants: dict[str, list[dict]] = {}    # each task version's mutants and what the verifier said of them
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
                   max_turns: int = 6, write: bool = False) -> claude.Reply:
        """One agent session on a tier, counted under it."""
        name = self.cfg.tiers.chat if tier == "chat" else tier
        reply = await sessions.ask(self.cfg.tier(name), system, prompt, schema, cwd=cwd, max_turns=max_turns, write=write)
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

    # ----- tier D -----

    async def decide(self, gate: str, choices: list[str], task: Task) -> Decision:
        return (await self.decide_gates(task, {gate: choices}))[gate]

    async def diagnose(self, task: Task, defect: str, why: str) -> Decision:
        """One request to the decision tier, before any repair is paid for. Its answer is not escalated: unsure, the
        repair goes ahead and finds out."""
        skill = skills.load("audit/diagnose")
        asked = skill.data["diagnose"]
        state = {**self._state(task), "what_was_found": f"{DEFECTS.get(defect, defect)}\n{why}"[:4000]}
        answers, cost, tokens = await jev.decisions(self.cfg.tiers.decision.model, state,
                                                    {"diagnose": jev.choice(asked["question"], asked["criteria"])})
        self.usage("decision", cost, tokens)
        return self._jev_decision(DIAGNOSES, answers.get("diagnose") or {}, cost, tokens, skill.tag)

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
        self.mutants[task.digest] = mutants        # run_mutant adds what the verifier said: a repair reads it
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
        said = (f"A first check, by a small model that saw only the start of long files, said \"{first.value or '(no answer)'}\" "
                f"with probability {first.prob:.2f}. It may be wrong: read the instruction and the tests in full before you answer. "
                "What the verifier sets up or checks about itself, and anything a correct answer made from the instruction "
                "alone would satisfy, is not unstated. If you answer \"unstated\", name in the rationale each thing the tests "
                "rely on that the instruction does not state, with the test that relies on it, so that exactly that can be "
                "added to the instruction.\n\n")
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

    # ----- the audits and the repair: they read the evidence in a folder of their own -----

    def _evidence(self, task: Task, trials: list[dict], copy_task: bool = True) -> tuple[Path, str]:
        """A folder with a copy of the task and each trial unpacked under trials/, and a listing of it for the prompt."""
        folder = self.scratch / f"evidence-{task.name}-{uuid.uuid4().hex[:6]}"
        if copy_task:
            shutil.copytree(task.path, folder / "task", ignore=shutil.ignore_patterns("__pycache__", ".git"))
        lines = []
        for t in trials:
            where = folder / "trials" / t["label"]
            where.mkdir(parents=True, exist_ok=True)
            try:
                with tarfile.open(t["archive"]) as tar:
                    tar.extractall(where, filter="data")
            except (OSError, tarfile.TarError) as err:
                lines.append(f"- trials/{t['label']}: its files could not be unpacked ({err})")
                continue
            outcome = "error: " + t["error"][:200] if t["error"] else ("passed" if t["passed"] else "failed")
            lines.append(f"- trials/{t['label']}/: {t['who']}, reward {t['reward']}, {outcome}")
        text = ("EVIDENCE\nThe current directory holds the task under task/" + (" and these trials:\n" + "\n".join(lines) if lines
                else ". No agent trial of this version of it is on this machine: judge from the task's own files."))
        return folder, text

    async def _judge(self, skill_name: str, tier: str, task: Task, trials: list[dict], choices: list[str]) -> Decision:
        skill = skills.load(skill_name)
        folder, evidence = self._evidence(task, trials)
        try:
            r = await self._ask(tier, skill.system, skill.fill(task=describe(task, 4000), evidence=evidence),
                                _choice_schema(choices, rationale=True), cwd=folder, max_turns=60)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        return Decision(r.data["value"], float(r.data["confidence"]), tier, self.cfg.tier(tier).model, r.cost, r.tokens,
                        r.data.get("rationale", ""), skill=skill.tag)

    async def audit(self, task: Task, round_no: int) -> Decision:
        """How the worker agent's trials went: the task's fault, or the agent's. Before any frontier trial."""
        if "audit" in self.assumed:
            d = await self.sim.audit(task, round_no)
            d.cost = 0.0
            return d
        trials = [t for t in self.kept.get(task.digest, []) if t["worker"]][:4]
        return await self._judge("audit/rubric", "worker", task, trials, AUDIT)

    async def second_opinion(self, task: Task, round_no: int) -> Decision:
        if "audit" in self.assumed:
            d = await self.sim.second_opinion(task, round_no)
            d.cost = 0.0
            return d
        trials = [t for t in self.kept.get(task.digest, []) if t["worker"]][:4]
        return await self._judge("audit/rubric", "frontier", task, trials, AUDIT)

    async def final_audit(self, task: Task) -> Decision:
        """After the difficulty run: the failed trials, read for whether the task is hard or broken."""
        if "audit" in self.assumed:
            d = await self.sim.final_audit(task)
            d.cost = 0.0
            return d
        failed = [t for t in self.kept.get(task.digest, []) if not t["worker"] and not t["passed"]][:4]
        return await self._judge("audit/final", "worker", task, failed, FINAL)

    async def repair(self, task: Task, defect: str, round_no: int, tier: str, why: str = "") -> dict:
        """An agent edits a copy of the task. A repair that changed something comes back with the copy's path: it is a new
        version, with a new digest, and every check runs on it again."""
        if self.assumed and defect != "oracle":      # a real verify failure is never repaired by assumption
            return {**await self.sim.repair(task, defect, round_no, tier), "cost": 0.0}
        skill = skills.load("audit/repair")
        copy = self.run_dir / "repaired" / f"{task.name}-r{round_no}" / task.name
        shutil.rmtree(copy.parent, ignore_errors=True)
        shutil.copytree(task.path, copy, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        said = f"{DEFECTS.get(defect, defect)}\n\nWhat was found: {why or defect}"
        notes = []
        if defect == "verifier":
            for m in self.mutants.get(task.digest, []):
                wrong = (m["kind"] == "wrong") == (float(m.get("reward") or 0) > 0)
                if wrong:
                    notes.append(f"Mutant {m['id']} ({m['kind']}): {m.get('what', '')} The verifier gave it reward {m.get('reward')}.\n"
                                 + "\n".join(f"--- solution/{path}\n{text[:4000]}" for path, text in m["files"].items()))
        trials = [t for t in self.kept.get(task.digest, []) if t["worker"]][:3]
        folder, evidence = self._evidence(task, trials, copy_task=False) if trials else (None, "")
        if folder is not None:
            shutil.move(str(folder / "trials"), str(copy.parent / "evidence"))
            shutil.rmtree(folder, ignore_errors=True)
            evidence = evidence.replace("The current directory holds the task under task/ and these trials:",
                                        "The worker agent's trials are unpacked in ../evidence/, read-only for you:").replace("trials/", "../evidence/")
        if notes:
            evidence += "\n\nTHE MUTANTS THE VERIFIER GOT WRONG\n" + "\n\n".join(notes)
        before = digest_dir(copy)
        r = await self._ask(tier, skill.system, skill.fill(defect=said, evidence=evidence), REPAIR_SCHEMA, cwd=copy,
                            max_turns=80, write=True)
        shutil.rmtree(copy.parent / "evidence", ignore_errors=True)
        changed = digest_dir(copy) != before
        fixed = bool(r.data.get("fixed")) and changed
        what = r.data.get("what", "") if changed or not r.data.get("fixed") else "said it was fixed, but changed no file"
        return {"fixed": fixed, "what": what, "cost": r.cost, "tokens": r.tokens, "skill": skill.tag,
                **({"path": copy} if fixed else {})}

    async def write_report(self, task: Task, blackboard: dict) -> dict:
        """The report is written by code, from the records: no model is called."""
        return {"cost": 0.0, "tokens": 0}

    # ----- Harbor -----

    def limits(self, task: Task | None = None, what: str = "") -> dict:
        """The protocol's caps on building an environment and on running a verifier, for every Harbor run, and with a
        task, the watch that stops a run of it that hangs."""
        caps = {"max_build_seconds": self.cfg.triage.max_build_seconds, "max_verifier_seconds": self.cfg.triage.max_verifier_seconds}
        if task is not None:
            caps["watch"] = harbor.Watch(self.cfg.run.watch_minutes * 60, self.cfg.run.quiet_minutes * 60,
                                         lambda log, quiet: self.stuck(task, what, log, quiet))
        return caps

    async def judge_stuck(self, what: str, log: str, quiet: float) -> Decision:
        skill = skills.load("audit/stuck")
        r = await self._ask("worker", skill.system, skill.fill(what=what, quiet=f"{quiet / 60:.0f}", log=log),
                            _choice_schema(["hung", "working"], rationale=True))
        return Decision(r.data["value"], float(r.data["confidence"]), "worker", self.cfg.tiers.worker.model, r.cost,
                        r.tokens, r.data.get("rationale", ""), skill=skill.tag)

    def _settle(self, result: TrialResult, keep: bool, label: str) -> TrialResult:
        return harbor.settle(result, keep, self.run_dir / "trials", re.sub(r"[^A-Za-z0-9._-]+", "-", label),
                             self.run_dir.parents[1])

    async def run_oracle(self, task: Task) -> TrialResult:
        r = await harbor.run(task.path, self.jobs, "oracle", **self.limits(task, "the reference solution's run"))
        return self._settle(r, keep=not r.passed or bool(r.error), label=f"{task.name}.oracle.{uuid.uuid4().hex[:6]}")

    async def run_noop(self, task: Task) -> TrialResult:
        r = await harbor.run(task.path, self.jobs, "nop", **self.limits(task, "the no-op run"))
        return self._settle(r, keep=r.passed or bool(r.error), label=f"{task.name}.noop.{uuid.uuid4().hex[:6]}")

    async def run_mutant(self, task: Task, mutant: dict, stage: str = "probe") -> bool:
        if stage in self.assumed:
            return await self.sim.run_mutant(task, mutant, stage)
        copy = harbor.with_solution(task.path, mutant["files"], self.scratch)
        try:
            result = await harbor.run(copy, self.jobs, "oracle", **self.limits(task, f"the run of wrong answer {mutant['id']}"))
        finally:
            shutil.rmtree(copy.parent, ignore_errors=True)
        mutant["reward"], mutant["error"] = result.reward, result.error
        finding = (mutant["kind"] == "wrong") == result.passed      # a wrong one accepted, or a right one rejected
        self._settle(result, keep=finding or bool(result.error), label=f"{task.name}.mutant-{mutant['id']}")
        return result.passed

    async def _agent_run(self, task: Task, entry: SuiteEntry, max_hours: float, mounts: list[dict]) -> TrialResult:
        return await harbor.run(task.path, self.jobs, entry.agent, entry.model,
                                {"version": entry.version, "reasoning_effort": entry.reasoning_effort}, with_keys=True,
                                max_agent_seconds=(max_hours or self.cfg.difficulty.max_agent_hours) * 3600,
                                agent_setup_seconds=self.cfg.run.agent_setup_minutes * 60, mounts=mounts,
                                agent_env=entry.env or None, **self.limits(task, f"a trial of {entry.agent} with {entry.model}"))

    async def run_trial(self, task: Task, entry: SuiteEntry, index: int, max_hours: float = 0) -> TrialResult:
        if "difficulty" in self.assumed:
            result = await self.sim.run_trial(task, entry, index)
            result.cost, result.tokens_in, result.tokens_out, result.seconds = 0.0, 0, 0, 0.0
            return result
        # The agent's program at its pinned version is downloaded once here and mounted into the container, in place of
        # an install of several minutes in every one. Should a trial fail in setup with it mounted (an image it cannot
        # run in), it is run once more the plain way.
        shared = await asyncio.to_thread(agents.mounts, entry.agent, entry.version)
        r = await self._agent_run(task, entry, max_hours, shared)
        if shared and r.error and "setup" in r.error.lower():
            r = await self._agent_run(task, entry, max_hours, [])
        worker = bool(max_hours)            # the worker agent's tries carry their own limit; the suite's use the protocol's
        who = f"{entry.agent} with {entry.model}" + (f", effort {entry.reasoning_effort}" if entry.reasoning_effort else "")
        label = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{'worker' if worker else 'frontier'}-{entry.agent}-{entry.reasoning_effort or 'default'}-{index}")
        r = self._settle(r, keep=True, label=f"{task.name}.{label}.{uuid.uuid4().hex[:4]}")
        if r.artifact:
            self.kept.setdefault(task.digest, []).append({
                "label": label, "archive": self.run_dir.parents[1] / r.artifact, "passed": r.passed, "reward": r.reward,
                "error": r.error, "worker": worker, "who": ("the worker agent: " if worker else "a frontier agent: ") + who})
        return r
