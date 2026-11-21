"""The admission gates that a model decides (DESIGN.md Section 4)."""
from __future__ import annotations

from .. import skills
from ..core.config import Config

# gate -> (the choices offered to the decision tier, the values that pass)
D_GATES: dict[str, tuple[list[str], set[str]]] = {
    "format": (["agentic", "multiple_choice", "question_answering", "short_answer"], {"agentic"}),
    "verifier": (["programmatic", "llm_judge", "rubric", "none"], {"programmatic"}),
    "verifier_cost": (["cheap", "costly"], {"cheap"}),
    "duplicate": (["distinct", "variant", "same"], {"distinct"}),
}


GATES_SKILL = "triage/gates"      # the questions and what each answer means: skills/triage/gates.toml


def wording() -> tuple[dict[str, str], dict[str, dict[str, str]], str]:
    """The question and the criteria for each gate, and the skill's tag for the ledger."""
    skill = skills.load(GATES_SKILL)
    return ({g: v["question"] for g, v in skill.data.items()}, {g: v["criteria"] for g, v in skill.data.items()}, skill.tag)


def passes(gate: str, value: str, cfg: Config) -> bool:
    if gate == "verifier" and value == "llm_judge" and cfg.triage.allow_llm_judge_verifier:
        return True
    return value in D_GATES[gate][1]
