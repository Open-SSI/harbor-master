"""The real backend: Claude for the model tiers, Harbor for every run.

The decision tier runs on Jev through OpenRouter (provider "openrouter"), and on nothing else: Jev
returns real probabilities and answers all of a task's gates in one request. The worker and
frontier tiers run on Claude, or on OpenAI models through Codex.

Built: the admission gates, pairwise comparison, answers, the audits of a task's reference solution and of
its tests against its instruction, Harbor runs of an agent, the audit of the worker agent's trials and its frontier second
opinion, the repair, and the audit of the frontier trials. The adapter builder lives in adapter.py. The
report is written by code from the records, with no model.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
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
from .base import CHECKS, Backend, Decision, NotImplementedBackend, TrialResult, Usage
from .simulated import SimulatedBackend

CLAUDE_PROVIDERS = sessions.PROVIDERS       # what an agent session can run on: Claude, or OpenAI models through Codex


TEST_CODE = (".sh", ".py", ".js", ".ts", ".r", ".jl", ".pl", ".rb")


def test_code(task: Task, limit: int = 6000, each: int = 3000) -> str:
    """The code of the task's tests, so that how it is graded is read, not guessed from file names: a grader that
    calls a model API is a model grader. Data files beside the tests are left out."""
    folder, parts = task.path / "tests", []
    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in TEST_CODE) if folder.is_dir() else []
    for f in sorted(files, key=lambda p: (p.name != "test.sh", str(p))):     # the entry point first
        text = f.read_text(errors="replace")
        parts.append(f"--- {f.relative_to(task.path)}\n" + (text if len(text) <= each else text[:each] + "\n[cut here]"))
    joined = "\n".join(parts)
    return joined if len(joined) <= limit else joined[:limit] + "\n[test code cut here]"


def describe(task: Task, limit: int = 6000, tests: bool = False) -> str:
    """What a model is shown about a task: its instruction, its metadata, its files, and with `tests` the code of its
    tests. Never the solution."""
    files = sorted(str(p.relative_to(task.path)) for p in task.path.rglob("*")
                   if p.is_file() and "solution" not in p.relative_to(task.path).parts and ".git" not in p.parts)
    meta = {**task.meta.get("task", {}), **task.meta.get("metadata", {})}
    for drop in ("authors", "author_email", "author_name", "author_profile", "_fixed"):
        meta.pop(drop, None)
    text = task.instruction if len(task.instruction) <= limit else task.instruction[:limit] + "\n[instruction cut here]"
    return (f"TASK {task.task_id}\n\nINSTRUCTION\n{text}\n\nMETADATA\n{json.dumps(meta, indent=1, default=str)[:2000]}\n\n"
            f"VERIFIER SETTINGS\n{json.dumps(task.meta.get('verifier', {}), default=str)}\n\n"
            f"FILES ({len(files)}, solution left out)\n" + "\n".join(files[:80]) +
            f"\n\nHas a reference solution: {task.has_solution}. Has tests: {task.has_tests}." +
            (f"\n\nTEST CODE\n{test_code(task)}" if tests and task.has_tests else ""))


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


RANK_ASK = {"timeout": 25, "attempts": 3}      # a ranking answer is one of hundreds: a slow one is not waited on long


class RankCache:
    """The decision model's ranking answers on this machine, kept in a file: a run that is stopped and started again,
    or that ranks again after screening changed, asks only what it has not asked before. Keyed by the model, the
    skill's version and everything the model was shown, so a changed brief or task card is asked afresh."""

    def __init__(self, path: Path):
        self.path, self._seen = path, None

    @staticmethod
    def key(model: str, tag: str, state: dict) -> str:
        return hashlib.sha256(json.dumps([model, tag, state], sort_keys=True).encode()).hexdigest()

    def _load(self) -> dict[str, float]:
        if self._seen is None:
            self._seen = {}
            with contextlib.suppress(OSError, ValueError):
                for line in self.path.read_text().splitlines():
                    k, v = json.loads(line)
                    self._seen[k] = float(v)
        return self._seen

    def __contains__(self, key: str) -> bool:
        return key in self._load()

    def __getitem__(self, key: str) -> float:
        return self._load()[key]

    def __setitem__(self, key: str, value: float) -> None:
        self._load()[key] = value
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps([key, value]) + "\n")


COST_RULE = """For this question, read the files that grade a submission: tests/test.sh, and the test files it runs. Answer
"costly" only from what you read there: grading needs a GPU or a paid API, or the tests themselves do slow work, such as
running a solver over many cases or a long simulation. A timeout setting is a limit, not how long grading takes: it is no
evidence on its own, and neither is the instruction's time allowed for the agent's own work. For "costly", give the file
you read and copy one line from it that shows the slow work. If you did not read the tests, or found nothing slow in them,
answer "cheap": the curation measures the real time of grading later, and drops a task whose grading is too slow."""


def cited(folder: Path, file: str, quote: str) -> str:
    """Why a cited line does not hold up, or "": the file must be the task's, and hold the line, as copied."""
    file, quote = (file or "").strip().lstrip("./"), " ".join((quote or "").split())
    if not file or not quote:
        return "it cited no file and line"
    path = (folder / file).resolve()
    if not path.is_file() or folder.resolve() not in path.parents:
        return f"it cited {file}, which the task does not have"
    try:
        text = " ".join(path.read_text(errors="replace").split())
    except OSError:
        return f"{file} could not be read"
    return "" if quote in text else f"the line it quoted is not in {file}"


def _choice_schema(choices: list[str], rationale: bool = False) -> dict:
    props = {"value": {"type": "string", "enum": choices},
             "confidence": {"type": "number", "description": "Probability from 0 to 1 that the value is right."}}
    if rationale:
        props["rationale"] = {"type": "string", "description": "One or two sentences citing the task's own text or files."}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


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


def _audit_schema(choices: list[str]) -> dict:
    """An audit's answer: the agent's own verdict, and what it found, its verdict and decisive points first."""
    return {"type": "object", "additionalProperties": False, "required": ["verdict", "summary"],
            "properties": {"verdict": {"type": "string", "enum": choices},
                           "summary": {"type": "string", "description": "The verdict and the decisive points first, then the minor "
                                       "ones, each with the file and lines it rests on."}}}
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
           "from_trial": "The task has no reference solution, and a frontier agent passed it: its trial is in the evidence. "
                         "Write solution/solve.sh that does what that agent did, from its steps in the trajectory: the same "
                         "commands, and the files it wrote, written out by the script, so that the tests pass in the task's "
                         "environment. Port the agent's own work. Never copy expected values out of the tests, and change "
                         "nothing outside solution/.",
           "oracle": "The reference solution does not pass its own tests, a no-op run passes, the reference passed once and failed "
                     "once, or a run failed for an infrastructure reason such as an image that does not build.",
           "verifier": "The verifier accepts a wrong answer, or rejects a correct one.",
           "design": "The tests check something the instruction does not state, or the task fails for a reason other than its difficulty.",
           "error": "A check of the task stopped with an error before it could judge the task. Read the error. If the task's own "
                    "files cause it (an environment that does not build, a missing file or dependency, a solution that crashes), "
                    "fix them. If they do not (a network timeout, a registry or service that did not answer, a model session that "
                    "gave no answer), change nothing and say fixed is false: the check is run again as it is."}


class RealBackend(Backend):
    """`assume_after` names the last stage that runs for real. Every later stage is assumed:
    its results come from the simulated backend, cost nothing, and are marked in the ledger."""

    def __init__(self, cfg: Config, usage: Usage, run_dir: Path, assume_after: str = ""):
        super().__init__(cfg, usage)
        self.run_dir, self.jobs, self.scratch = run_dir, run_dir / "harbor", run_dir / "scratch"
        self.assumed = set(LATER[LATER.index(assume_after) + 1:]) if assume_after else set()
        self.sim = SimulatedBackend(cfg, lambda tier, cost, tokens: None, speed=0)
        self.kept: dict[str, list[dict]] = {}       # each task version's agent trials, by digest: what the audits read
        self.ranked = RankCache(run_dir.parents[1] / "cache" / "ranking.jsonl")     # answers kept for a restart
        harbor.remove_leftovers(run_dir.parents[1])         # containers an earlier run here left running
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
        return {"task": describe(task, 6000, tests=True)[:24000]}

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
        prompt = skill.fill(question=questions[gate], first_value=first.value or "(no answer)",
                            first_prob=f"{first.prob:.2f}", task=describe(task))
        schema = _choice_schema(choices, rationale=True)
        if gate == "verifier_cost":
            prompt += "\n\n" + COST_RULE
            schema["properties"]["evidence_file"] = {"type": "string", "description": "The task file you read that shows grading "
                                                     "is slow, relative to the task's folder. Empty for cheap."}
            schema["properties"]["evidence_quote"] = {"type": "string", "description": "One line copied exactly from that file. "
                                                      "Empty for cheap."}
            schema["required"] = list(schema["properties"])
        r = await self._ask("worker", skill.system, prompt, schema, cwd=task.path, max_turns=16)
        d = Decision(r.data["value"], float(r.data["confidence"]), "worker", self.cfg.tiers.worker.model,
                     r.cost, r.tokens, r.data.get("rationale", ""), skill=skill.tag)
        if gate == "verifier_cost" and d.value == "costly":
            why = cited(task.path, r.data.get("evidence_file", ""), r.data.get("evidence_quote", ""))
            if why:         # a costly with nothing read behind it drops nothing: verify measures the real time
                return Decision("cheap", d.prob, "worker", d.model, d.cost, d.tokens,
                                f"Not dropped: {why}. It said: {d.rationale}", skill=d.skill)
            d.evidence = f"{r.data['evidence_file']}: {r.data['evidence_quote'].strip()}"[:400]
        return d

    async def importance(self, brief: str, task: Task) -> float | None:
        skill = skills.load("rank/score")
        state = {"brief_from_the_field_lead": brief or "(none given)", "task": card(task)}
        key = self.ranked.key(self.cfg.tiers.decision.model, skill.tag, state)
        if key in self.ranked:
            return self.ranked[key]
        q = {"importance": {"type": "score", "instructions": skill.data["question"], "criteria": skill.data["levels"]}}
        try:
            answers, cost, tokens = await jev.decisions(self.cfg.tiers.decision.model, state, q, **RANK_ASK)
        except RuntimeError:
            return None
        self.usage("decision", cost, tokens)
        answer = answers.get("importance") or {}
        score = float(answer["score"]) if answer.get("type") == "score" and "score" in answer else None
        if score is not None:
            self.ranked[key] = score
        return score

    async def compare(self, brief: str, a: Task, b: Task) -> float:
        skill = skills.load("rank/compare")

        async def ask(first: Task, second: Task) -> float:
            state = {"brief_from_the_field_lead": brief or "(none given)",
                     "first_task": card(first), "second_task": card(second)}
            key = self.ranked.key(self.cfg.tiers.decision.model, skill.tag, state)
            if key in self.ranked:
                return self.ranked[key]
            q = {"first_matters_more": jev.noul(skill.data["question"], skill.data["yes"], skill.data["no"])}
            try:
                answers, cost, tokens = await jev.decisions(self.cfg.tiers.decision.model, state, q, **RANK_ASK)
            except RuntimeError:        # no answer in time: this pair says nothing, and the ranking does not wait for it
                return 0.5
            self.usage("decision", cost, tokens)
            got = (answers.get("first_matters_more") or {}).get("noul")
            p = min(0.99, max(0.01, float(got if got is not None else 0.5)))
            if got is not None:
                self.ranked[key] = p
            return p

        ab, ba = await asyncio.gather(ask(a, b), ask(b, a))      # both orders, to cancel position bias, side by side
        return (ab + (1 - ba)) / 2

    # ----- tier W -----

    async def reported(self, benchmark: str, repo: Path, names: list[str]) -> dict:
        skill = skills.load("triage/reported-results")
        listed = "\n".join(names[:1500]) + (f"\n(and {len(names) - 1500} more: they are named like these)" if len(names) > 1500 else "")
        r = await self._ask("worker", skill.system, skill.fill(benchmark=benchmark, n=len(names), names=listed),
                            REPORTED_SCHEMA, cwd=repo, max_turns=40)
        tasks = {t["task"]: {"pass_rate": t["pass_rate"], "model": t.get("model", "")} for t in r.data.get("tasks", [])}
        return {"tasks": tasks, "source": r.data.get("source", ""), "cost": r.cost, "tokens": r.tokens, "skill": skill.tag}

    async def check_audit(self, task: Task, check: str, tier: str, earlier: list[Decision] | None = None) -> Decision:
        c = CHECKS[check]
        if c["stage"] in self.assumed:
            d = await self.sim.check_audit(task, check, tier, earlier)
            d.cost = 0.0
            return d
        skill = skills.load(c["skill"])
        before = "".join(f"\n\nAN EARLIER AUDIT, by the {e.tier} model, whose verdict was \"{e.value}\". The decision on it was "
                         f"unsure, or went against that verdict. Read the files yourself and take nothing in it on trust:\n{e.rationale}"
                         for e in (earlier or []))
        prompt = f"{skill.data['agent']}\n\nThe task's files, its solution and tests among them, are in the current directory.{before}\n\n{describe(task)}"
        r = await self._ask(tier, skill.data["system"], prompt, _audit_schema(c["choices"]), cwd=task.path,
                            max_turns=40 if tier == "frontier" else 30)
        return Decision(r.data["verdict"], 0.0, tier, self.cfg.tier(tier).model, r.cost, r.tokens, r.data["summary"], skill=skill.tag)

    async def check_decide(self, task: Task, check: str, audits: list[Decision]) -> Decision:
        c = CHECKS[check]
        if c["stage"] in self.assumed:
            d = await self.sim.check_decide(task, check, audits)
            d.cost = 0.0
            return d
        skill = skills.load(c["skill"])
        state = {"task": describe(task, 4000)[:8000],
                 "audits": [{"by": f"the {a.tier} model", "verdict": a.value, "found": a.rationale[:8000]} for a in audits]}
        answers, cost, tokens = await jev.decisions(self.cfg.tiers.decision.model, state,
                                                    {check: jev.choice(skill.data["question"], skill.data["criteria"])})
        self.usage("decision", cost, tokens)
        return self._jev_decision(c["choices"], answers.get(check) or {}, cost, tokens, skill.tag)

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
            # Where its steps and its grading are, so that the trajectory is read, not only what the tests said
            for what, pattern in (("its steps", "agent/trajectory.json"), ("the agent's own output", "agent/*.txt"),
                                  ("what the tests said", "verifier/test-stdout.txt")):
                found = sorted(where.glob(f"**/{pattern}"))[:1]
                if found:
                    lines.append(f"    {what}: {found[0].relative_to(folder)}")
        text = ("EVIDENCE\nThe current directory holds the task under task/" + (" and these trials. Read each one's steps, not only "
                "what the tests said: a judgment of how an agent failed that reads no trajectory is not one.\n" + "\n".join(lines) if lines
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

    def carry(self, old: str, new: str) -> None:
        self.kept[new] = list(self.kept.get(old, [])) + [k for k in self.kept.get(new, []) if k not in self.kept.get(old, [])]

    def remember(self, task: Task, trial: dict) -> None:
        """A trial in the ledger whose archive is still on this machine (`artifact`, under .hm/), made by an earlier run:
        the audits read it as they read this run's own. Without this, a task that resumes at the audit or the report
        is judged with no trajectory at all."""
        archive = self.run_dir.parents[1] / trial.get("artifact", "") if trial.get("artifact") else None
        if archive is None or not archive.is_file():
            return
        kept = self.kept.setdefault(trial["digest"], [])
        if any(Path(k["archive"]) == archive for k in kept):
            return
        worker = trial.get("purpose") == "cheap_solve"
        effort = trial.get("reasoning_effort") or ""
        who = f"{trial.get('agent', '')} with {trial.get('model', '')}" + (f", effort {effort}" if effort else "")
        kept.append({"label": archive.name.removesuffix(".tar.gz"), "archive": archive, "passed": bool(trial.get("passed")),
                     "reward": trial.get("reward"), "error": trial.get("error") or "", "worker": worker,
                     "who": ("the worker agent: " if worker else "a frontier agent: ") + who})

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
        trials = ([t for t in self.kept.get(task.digest, []) if not t["worker"] and t["passed"]][:1] if defect == "from_trial"
                  else [t for t in self.kept.get(task.digest, []) if t["worker"]][:3])
        folder, evidence = self._evidence(task, trials, copy_task=False) if trials else (None, "")
        if folder is not None:
            shutil.move(str(folder / "trials"), str(copy.parent / "evidence"))
            shutil.rmtree(folder, ignore_errors=True)
            evidence = evidence.replace("The current directory holds the task under task/ and these trials:",
                                        "The passing trial is unpacked in ../evidence/, read-only for you:" if defect == "from_trial" else
                                        "The worker agent's trials are unpacked in ../evidence/, read-only for you:").replace("trials/", "../evidence/")
        if defect == "from_trial" and not trials:      # its archive is not on this machine: nothing to port
            shutil.rmtree(copy.parent, ignore_errors=True)
            return {"fixed": False, "what": "the passing trial's files are not on this machine", "cost": 0.0, "tokens": 0,
                    "skill": skill.tag}
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
                                         lambda log, quiet: self.stuck(task, what, log, quiet),
                                         say=lambda doing, ran: self.on_progress and self.on_progress(task, what, doing, ran))
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

    async def _agent_run(self, task: Task, entry: SuiteEntry, max_hours: float, mounts: list[dict], what: str = "") -> TrialResult:
        """One agent trial. While it runs, its cost so far is reported each minute, so that what is spent keeps up with
        it; when it ends, that running figure gives way to the trial's own cost, which the trial's record carries."""
        key, caps = uuid.uuid4().hex, self.limits(task, what or f"a trial of {entry.agent} with {entry.model}")
        if self.on_spend is not None:
            caps["watch"].spend = lambda cost: self.on_spend(key, cost)
        try:
            return await self._agent_trial(task, entry, max_hours, mounts, caps)
        finally:
            if self.on_spend is not None:
                self.on_spend(key, None)

    async def _agent_trial(self, task: Task, entry: SuiteEntry, max_hours: float, mounts: list[dict], caps: dict) -> TrialResult:
        return await harbor.run(task.path, self.jobs, entry.agent, entry.model,
                                {"version": entry.version, "reasoning_effort": entry.reasoning_effort}, with_keys=True,
                                max_agent_seconds=(max_hours or self.cfg.difficulty.max_agent_hours) * 3600,
                                agent_setup_seconds=self.cfg.run.agent_setup_minutes * 60, mounts=mounts,
                                agent_env=entry.env or None, **caps)

    async def run_trial(self, task: Task, entry: SuiteEntry, index: int, max_hours: float = 0) -> TrialResult:
        if "difficulty" in self.assumed:
            result = await self.sim.run_trial(task, entry, index)
            result.cost, result.tokens_in, result.tokens_out, result.seconds = 0.0, 0, 0, 0.0
            return result
        # The agent's program at its pinned version is downloaded once here and mounted into the container, in place of
        # an install of several minutes in every one. Should a trial fail in setup with it mounted (an image it cannot
        # run in), it is run once more the plain way.
        shared = await asyncio.to_thread(agents.mounts, entry.agent, entry.version)
        worker = bool(max_hours)            # the worker agent's tries carry their own limit; the suite's use the protocol's
        what = (f"the worker agent's try {index + 1} ({entry.model})" if worker else
                f"difficulty try {index + 1} of {self.cfg.difficulty.trials} by {entry.agent} ({entry.model})")
        r = await self._agent_run(task, entry, max_hours, shared, what)
        if shared and r.error and "setup" in r.error.lower():
            r = await self._agent_run(task, entry, max_hours, [], what)
        who = f"{entry.agent} with {entry.model}" + (f", effort {entry.reasoning_effort}" if entry.reasoning_effort else "")
        label = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{'worker' if worker else 'frontier'}-{entry.agent}-{entry.reasoning_effort or 'default'}-{index}")
        r = self._settle(r, keep=True, label=f"{task.name}.{label}.{uuid.uuid4().hex[:4]}")
        if r.artifact:
            self.kept.setdefault(task.digest, []).append({
                "label": label, "archive": self.run_dir.parents[1] / r.artifact, "passed": r.passed, "reward": r.reward,
                "error": r.error, "worker": worker, "who": ("the worker agent: " if worker else "a frontier agent: ") + who})
        return r
