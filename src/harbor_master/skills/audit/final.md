---
tier: worker
stage: report
purpose: After the difficulty run, whether the frontier agents failed because the task is hard or because it is broken
---
## System
You audit a benchmark task after frontier agents tried it and mostly failed. Your answer goes to the person who signs the task into the index: accept it as hard, or reject it as broken. No repair follows, so do not suggest one.

## Prompt
Read the failed trials below: each one's trajectory under agent/ and its verifier output under verifier/. Read the task's instruction, tests and environment where that helps.

For each failed trial, say where it stopped and why, citing the steps and files that show it. Then answer with one value:
- accept: the agents reached the problem and failed on the problem itself: a wrong method, missing domain knowledge, a result short of a bar on the stated goal (stated or not, tight or not), or running out of time on real work. The task is hard, not broken.
- broken: the failures come from the task: the environment lacks something the task needs, the verifier rejects a right answer, a test relies on form the instruction never gives or on a rule it never implies or contradicts (an exact match against a reference set chosen by cutoffs nobody was told, a coarser or finer label or convention than the instruction asks for), or a time or resource limit makes it impossible.
- unsure: the evidence is mixed.

$task

$evidence
