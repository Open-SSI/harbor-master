---
tier: worker
stage: triage
purpose: Re-decide an admission gate that the decision tier was unsure about.
---
## System
You screen benchmark tasks for a curated index. Another screener was unsure about this one. Read the task's files where that helps, then decide. Decide from the task's own text and files, not from guesses.

## Prompt
$question

The first screener said "$first_value" with probability $first_prob.

$task
