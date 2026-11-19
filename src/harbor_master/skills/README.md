# Skills

What each agent is told. One file per job, grouped by stage. Code holds the rules
(which answers pass, what a stage does with a result); these files hold the wording.

The curation skills (`triage/`, `rank/`, `probe/`, `audit/`) decide what gets into an index.
Each index has its own copies under `skills/` in its repository: `hm index new` writes them,
and they are locked with the index's protocol. The files here are the templates those copies
start from. An index may change any wording. It may not change what the code reads: the keys
of a TOML skill, and the `$placeholders` and the two sections of a prompt. `hm skills` shows
where an index stands, `hm skills diff` what differs, and `hm skills adopt` takes a template in.

The session skills (`session/`) read what is typed on the run screen. They change no result,
so they are the same for every index and are not part of any protocol.

| File | Read by | For |
|---|---|---|
| `triage/gates.toml` | decision tier (Jev) | The admission gates: each question and what each answer means |
| `triage/decide.md` | decision tier, when it runs on a general model | One gate at a time |
| `triage/confirm.md` | worker tier | Re-deciding a gate the decision tier was unsure about |
| `rank/score.toml` | decision tier | How much one task matters under the lead's brief, on an ordered scale |
| `rank/compare.toml` | decision tier | Which of two tasks matters more under the lead's brief |
| `probe/write-mutants.md` | worker tier | Variants of the reference solution that test the verifier |
| `audit/rubric.md` | worker tier | The task-quality rubric. Not used yet |
| `session/interpret.toml` | decision tier | Reading a plain-text message as a command |
| `session/answer.md` | chat tier | Answering a question from the records |

A `.md` skill has a `## System` and a `## Prompt` section, with `$name` placeholders that
the code fills in. A `.toml` skill is data for a decision model: questions and criteria.

**Versions.** A skill's version is a short hash of its file. Every ledger record made
with a skill names it and its version, for example `triage/gates@3f2a91c0d4`. So for any
decision that was later overturned, the exact wording that produced it is known. That is
what makes these files improvable by measurement: change one, run, and compare overturn
rates between versions. Doing that automatically is future work.

Edit a skill by editing its file. Nothing else needs to change.
