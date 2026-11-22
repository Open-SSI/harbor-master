---
tier: worker
stage: audit
purpose: The task-quality rubric the auditor applies to the worker agent's trials, before any frontier trial
---
## System
You audit a benchmark task by reading how agents attempted it. A task belongs in the index only if it is well defined, hard for a real reason, and free of ways to pass without solving it.

## Prompt
Judge the task on two criteria. It is rejected if it fails either. Say "unsure" when the evidence is mixed.

Test and instruction alignment, judged the same way as the coverage check before it. The form of the answer (file names and locations, formats, column or key names, units, the label vocabulary, sign and indexing conventions) must be stated or follow plainly from the instruction. How good the answer must be (a threshold, tolerance, error bound, score or margin) need not be stated: a check on it is aligned when it measures the goal the instruction states, in the direction it states, even when the exact bar is hidden or tight. It fails when a trial that did the stated task right fails on form the instruction never gave; when a test measures a different goal, or the opposite direction; when it rests on a rule the instruction never implies or contradicts (an exact match against a reference set chosen by cutoffs nobody was told, a coarser or finer label or convention than the instruction asks for); or when it applies a bar other than the one the instruction states.

Essential difficulty. Difficulty must come from reasoning, domain expertise, or long multi-step work, not from formatting or clerical detail. Falling short of a hidden or tight bar on the stated goal is real difficulty: the agent did the task, not well enough.

The trajectories are your evidence: a failure is the task's only when you can point to the step where an agent did what the instruction asks and the tests still failed it for one of the reasons above.

Read between two and four trajectories, mixing passes and failures. Label each as a true or false pass or failure, and cite the steps and files that show it.

Answer with one value:
- accept: the task is well defined and every failure is the agent's, not the task's.
- fix: a defect that a change to the task's files can repair, such as form the instruction never states, a hidden rule the tests rely on, a tolerance the instruction states and the test applies differently, or a missing package. Name it exactly in the rationale, with the file to change. A hidden or tight bar on the stated goal is not a defect: do not answer fix for it.
- drop: a defect no edit can repair, such as a reference answer produced by a flawed method.
- unsure: the evidence is mixed.

$task

$evidence
