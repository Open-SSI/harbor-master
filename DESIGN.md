# Harbor-Master design

Harbor-Master takes a task or a benchmark in any format and returns a decision a person can sign: accept into an index, or drop, with the evidence. It holds no index of its own. Each index is a repository that says what it curates and how (Section 9). Section 13 says what is built, and Section 14 what is open.

## 1. What the Harbor process costs today

Source: Harbor Adapters and Harbor-Index, arXiv 2609.04298v3.

| Step in the paper | Who | Cost |
|---|---|---|
| Adapter creation, oracle check, parity (3 runs per side) | Contributor with a coding agent | Engineer time per benchmark |
| Review | Bot, then 1 to 3 reviewers, then a lead: over 10,000 GitHub comments | Human time |
| Difficulty filter: 18 frontier trials per task | Frontier models | 6,627 tasks in, 1,311 out |
| AI audit of 2 to 4 trajectories | Small model | 1,311 in, 307 out |
| Human review, panel, audit-and-fix loop | 14 reviewers, seniors repair by hand | 307 in, 82 out |

Frontier trials are spent before any audit, so broken tasks consume most of them. People do the repair. And it ran once. Harbor-Master puts cheap checks first, has agents repair, and never runs anything twice.

## 2. Three model tiers

| Tier | Default | Does |
|---|---|---|
| D, decision | `typesafe/jev-1.14` through OpenRouter | Every gate and routing choice: a typed value with a probability. It cannot write prose or code |
| W, worker | `claude-sonnet-5-5`, Claude Agent SDK | Adapters, repairs, audits, reports, drafting a benchmark's entry |
| F, frontier | `claude-opus-5-5` | Second judge on a contested audit, repair after W failed twice |

The worker and frontier tiers run on Claude models (provider `claude-agent-sdk`) or on OpenAI models (provider `codex`, through the Codex SDK, with an OpenAI API key or a ChatGPT subscription sign-in). A Codex session is read-only. The decision tier runs on Jev only, because a general model states a confidence, not a probability.

**Costs.** A cost that a provider reports is recorded as it is. Where none is reported, as with the Codex SDK and some agents on a subscription, the session or trial is costed from its tokens at the provider's published rate (`core/prices.py`: fresh input, cached input, cache writes, output), so it still counts in what was spent. A model with no list price there is recorded at 0.

A stage calls the next tier up only on a recorded trigger: low confidence, disagreement between two checks, or a retry limit. The share of tasks that reach tier F is reported for every run. The frontier models that are the *subjects* of the difficulty filter are counted apart. There is no budget: nothing stops a run on what it spends, and the screens show what was spent.

## 3. Intercom: typed records, no agent chat

Agents never message each other. Each stage reads a typed record and writes one, plus pointers to files on disk. The scheduler is ordinary code over those records.

- A tier D call is a schema and the text to read. It returns a value and a probability.
- Under the stage's threshold, the same schema goes to tier W, which adds a written rationale, and from there to tier F or a person.
- A W or F session ends in one JSON object that fits the stage's schema.

So every hand-off can be cached, compared across tiers, and checked by someone else.

## 4. Stages

| # | Stage | Runs on | Decides or produces | A task leaves when |
|---|---|---|---|---|
| 0 | Intake | Code | Fetches at the pinned commit, computes digests | |
| 0b | Check | Code and a registry | The cheap checks on the whole benchmark (Section 9): asked when it is added, and again before its adapter, once per pinned release | A check fails and a person confirms |
| 1 | Adapt | W, then F, then a person. Once per benchmark, skipped for Harbor-format input | An adapter that converts every task, with Harbor's own adapter guide, mending what is broken for every task (Section 9) | The person abandons the benchmark |
| 2 | Triage | D, then W when unsure | The admission gates below. A task with no reference solution passes: one is written if it is selected | A gate fails |
| 3 | Verify | D, then W or F, then code | Makes the selected task valid: a missing reference solution is written, a failing one repaired, after the decision model says what would make it valid or that nothing would. Then the reference passes twice, a no-op fails, trivial submissions fail, the verifier is fast enough | Nothing would make it valid, or still failing after the retry limit |
| 4 | Probe | W and code, then D | Mutants of the solution test the verifier. Then one question: does the instruction state everything the tests check | The verifier or the instruction is wrong and repair fails |
| 5 | Worker tries | The worker agent (Sonnet on Claude Code) | Two tries, side by side, each at most `cheap_agent_hours` | It solves the task both times: too easy |
| 6 | Audit | W; F when unsure | Reads how the worker agent failed, and says whether it was the task's fault, the agent's, or the infrastructure's | Broken and not repairable |
| 7 | Fix | W in round 1, F in round 2 | Repairs the task. The new version goes back to stage 3, through every cheap check again | Still asked for a fix after 2 rounds |
| 8 | Difficulty | The frontier suite | Last, once per task, outside the loop: 2 frontier agents x 3 trials, in rounds, each at most 4 hours | More than 3 of the 6 pass |
| 9 | Report | W | For a kept task only: audits how the frontier agents failed, then one report: evidence, diffs, cost, and a recommendation, reject when the failures are the task's fault. A task the workflow drops gets no report and no box: it is dropped, and its work cell takes the next task | |
| 10 | Verdict | A person | For a kept task: `accept [why]` or `reject <why>` (a rejection needs a reason), signed and credited | |

**One work cell, one task.** Stages 3 to 7 are the cheap loop: verify, probe, the worker's tries, audit, and a fix that sends the new version back to verify. A task leaves the loop when it passes all of them, is dropped, or runs out of repair rounds. Only then does stage 8, the frontier difficulty run, happen: once, on the version that passed, and nothing loops back from it. So the version that is reported, and accepted, is the version whose difficulty was measured.

**Public environments.** Right after a benchmark is fetched, before any adapter is written or any task screened, the container images its files name (Dockerfiles, compose files, task records) are looked up in their registries, one per repository. When every one is refused, the benchmark is to be rejected. When some are, whoever writes the adapter is told which, and to leave those tasks out.

**A person confirms every rejection before curating.** A benchmark turned away for any reason, such as no public environment, a fetch that fails, an adapter no model could write, or no task passing the gates, is put to a person in a box, who only confirms what follows from the reason: a lasting one (no public environment, no adapter, no task passing the gates) drops it, a passing one (a failed fetch or model call) leaves it out of this run. With nobody to ask, nothing is dropped. **No task accepted** drops it too: when a run is over and the last verdict leaves none of its chosen tasks accepted (the workflow kept none, or every kept one was rejected), it is dropped and a box says so.

**Admission gates.** A task is kept only if all hold. Each failure has its own reason code, and the gates apply per task.

| Gate | Keeps | Settled by |
|---|---|---|
| Format | Tasks where an agent executes work, not multiple choice or short answers | D |
| Reference solution | Not a gate: one that is missing is written in stage 3 for a selected task | Stage 3 |
| Verifier | Programmatic and deterministic, not judged by a model or a person | D, then stage 3 |
| Verifier cost | Finishes within `max_verifier_seconds` on CPU, with no paid API. Every Harbor run is capped at it: one that takes longer fails, and the task is dropped | D estimates, stage 3 measures |
| Build time | The environment builds within `max_build_seconds`. Every Harbor run is capped at it: one that takes longer fails, and the task is dropped, never repaired | Stage 3 |
| Reported results | No model is already reported to solve the task more than `max_reported_pass_rate` of the time. A task with no reported result passes | W, as an agent with read-only tools in the benchmark's repository, reads its own per-task results once per benchmark (a score for the whole benchmark or a category does not count); code drops only the tasks over the limit |
| Duplicate | Not in the ledger already | Code, D for near matches |

**Ranking.** The rank comes from comparisons, because scores on a short scale tie.

1. *The lead's brief*: a few sentences on what matters in this benchmark, stored as a signed `brief` record.
2. *A score for every task*: tier D reads the brief and a short card of the task. One request per task.
3. *Pairs near the top*: the best `pool_factor` times the cap are compared in pairs, each pair in both orders, about 12 per task, and a Bradley-Terry model turns the results into a rating.
4. *A contributor approves the selection* before any trial is spent. The ranked tasks are shown in the order they are taken, each with a line on what it asks, on a page that opens by itself and has to be answered: a number k approves the top k, up to the cap, and a new brief ranks again.

Measured on the 70 tasks of Terminal-Bench-Science: 468 requests, $0.023, 11 seconds. The pool depends on the cap, not on the benchmark's size.

**Selection cap.** Only the top `min(top_k, max_fraction x N)` tasks of a benchmark go on to the costly stages: 24 of 1,000, 18 of 70. Selection goes round-robin over sub-areas. The rest are `not_selected`, not dropped.

**Cheap checks before the costly one.** Stage 8 is where the money goes, so everything that can fail a task for cents or a few dollars runs before it, in the loop, and stage 8 runs once, last.

- *Trivial submissions* (stage 3, code). Besides the no-op, the task's output files are left empty, filled with a placeholder, and copied from a file that was already there. The output files are those the instruction and the tests both name. A verifier that accepts one goes to repair.
- *Coverage* (stage 4, one tier-D request, tier W when unsure). If the tests rely on a file name, format, unit or tolerance the instruction never states, the instruction is repaired here, not after the trials.
- *Worker tries* (stage 5). The worker agent tries `cheap_trials` times side by side, each for at most `cheap_agent_hours`. If every try passes, the task is dropped as `solved_by_worker` and no frontier trial is spent. One pass among failures is not enough. Its failures are what the audit reads.
- *Once per task, last.* Stages 3 to 6 loop through repair until they pass. The frontier trials come after the loop and are never run a second time; no repair follows them.

**Early stop** in stage 8: with 6 trials and a 50% threshold, a task is kept when 0 to 3 pass. It is dropped as soon as 4 pass, and kept as soon as 3 fail.

**The final audit.** No repair follows the difficulty run, so its failures are audited only to advise the reviewer: the worker reads how the frontier agents failed and says whether that was the agents' doing (the task is hard), the task's (a broken environment, a missing file, an unclear check), or unclear. The report leads with the recommendation, accept, reject, or read the trials first, and with why. The reviewer decides.

**The final cut.** Accepting is each reviewer's call. Bringing the accepted set down to the index's final size is a judgement over the whole set: difficulty, diversity, and no few benchmarks dominating. It is made by hand, by the owner and by the contributors the owner names as leads (`role = "lead"`, set with `hm contributors role HANDLE lead`). The dashboard's Tasks button shows the accepted tasks and how many each benchmark has. `hm cut TASK <why>` signs a `cut` verdict: the task leaves `index.toml` and its archive is deleted from the task store. The cutter's machine keeps a copy, so `hm restore TASK` can put it back. The ledger check before every push rejects a cut signed by anyone else.

## 5. Where this goes beyond the paper

1. **Verifier probes before frontier trials.** A worker writes mutants of the reference solution: some wrong in a way that matters, some right in another form. The verifier must reject the first and accept the second.
2. **Calibrated escalation.** Every decision is stored with its probability, and every later overturn is a label. Thresholds can be refit on those labels.
3. **Sequential difficulty filter**, run once per task after a smaller model has tried, with early stop and reuse of any valid trial in the ledger.
4. **Content-addressed record.** A repair changes a digest, so the cheap checks that depend on it run again. The frontier trials do not.
5. **Credited human judgement.** A verdict carries who signed it and why, and is never asked for twice.

## 6. Working cells and parallelism

- **Once per benchmark**: fetching, the adapter, triage, the brief, ranking and selection. Triage is one batched pass, so a task that fails a gate never takes a cell.
- **One working cell per task in flight**: verify, probe, difficulty, audit, fix, report. `cells = 4` means four tasks at once. A cell holds one agent session at a time, and runs containers side by side where runs are independent: the mutants, and the suite's agents within a round.
- **Limits**: `cells`, `max_containers`, and a rate limit per provider. One asyncio process holds a semaphore for each. To scale past one machine, run on more machines against the same ledger: claims (Section 7) keep them apart.

## 7. Records

Nothing goes to Harbor during curation. Three stores hold the work.

| Store | Holds |
|---|---|
| The index's repository (git) | The index's definition, the ledger of small signed records, the reports |
| The task store (Section 8) | The accepted tasks, each as one archive |
| `.hm/runs/` on each machine | Trajectories, verifier output, logs. Not shared |

### IDs

- **Benchmark ID**: its short `id` on the index's list, such as `tb-science`.
- **Task ID**: `<benchmark ID>/<task name>`, stable for the life of the task.
- **Revision**: the content digest of the task's folder. A repair keeps the ID and gives a new digest, and every record names both.

### Events

Every record has `id`, `time`, `runner` (the GitHub handle of who made it), `protocol` (the settings hash), a signature, and the `id` of the one before it in its file.

| Record | Written when |
|---|---|
| `benchmark` | Someone puts a benchmark on the list, or takes it off |
| `contributors` | The owner signs the contributor list |
| `submission` | A run takes in a benchmark's tasks, with their digests |
| `decision` | Any gate, the ranking, an adapter that passed, or a benchmark dropped because none could be made |
| `trial` | Any run of an agent or the verifier on a task: purpose, agent and version, model, pass, tokens, cost |
| `repair`, `report`, `verdict` | The fix loop, the end of curation, a person's judgement |
| `claim` | A run takes a benchmark, renews it while it works, and lets it go when it ends |
| `brief` | A lead says what matters |
| `usage`, `skip` | What the tiers were called for, and a benchmark a run left out (Section 11) |

### Skills: what each agent is told

Every prompt and criterion is a file, grouped by stage. Code holds the rules, skills hold the wording. The curation skills (`triage/`, `rank/`, `adapt/`, `probe/`, `audit/`) decide what gets into an index, so each index keeps its own copies under `skills/`, and they are part of the protocol hash. The session skills (`session/`: reading a typed line, answering a question, drafting a benchmark's entry) change no result and stay in the tool.

A skill's version is a hash of its file, and every record made with one names it, for example `triage/gates@dee91cd340`. So two wordings can be compared on the same tasks by their overturn rates and cost.

### Credit

Computed from the ledger: per person, tokens and dollars contributed, trials run, benchmarks added, repairs, verdicts. The dashboard's Credits button shows it.

### Never run twice

A trial slot is (task digest, agent and version, model, effort, trial index). A run refuses a filled slot and shows the existing result. Decisions and verdicts work the same way. Agent versions are pinned: each trial records the version the agent itself reported, and the ledger check before every push rejects a trial whose two versions differ.

### Two people at once

- **A benchmark is curated by one person at a time.** Before any work on it, a run pulls and writes one `claim` on the whole benchmark, with an expiry (`claim_hours` in `hm.toml`, 12), and pushes. Progress keeps it: each finished trial or task renews the claim once half its time is gone, so a long curation keeps its benchmark for as long as it moves. With no progress for `claim_hours`, the claim runs out and anyone may take the benchmark; if the run moves again and nobody has, it claims it again, and if someone has, it drops its queued tasks of that benchmark and leaves it to them. A run lets its claims go when it ends. A claim belongs to a person on a machine: each machine has a short random name, kept with the identity, so the same person on a second machine is told the first one holds it, as anyone else would be.
- **The push is the lock.** Git refuses a push when someone pushed first. The refused run pulls, sees the other claim, and leaves the benchmark to them.
- **Nothing about single tasks is shared while a run works.** No task is claimed, and no task's records are pushed as it finishes. A run pushes at five points only: its claim on a benchmark, a report that is ready (so that a reviewer on another machine can read and sign it), a signed verdict, the claim's renewal, and the end of the run, when everything else goes out. So the dashboard shows nobody's work in progress, only outcomes.
- **A rerun is refused.** A task that has an outcome under this protocol is not run again. One that failed or was stopped has none, so it is free for anyone.

### Concurrent writers

Each run appends to its own file, `ledger/<benchmark>/<runner>-<run id>.jsonl`, with its own hash chain, so git never sees a conflict. A task's state is a fold over its records: for automated decisions the higher tier wins, and human verdicts are never overwritten.

### What is not solved by this

- **Trust.** A contributor could submit trials that never ran. Records are accepted only from listed people, signed. That is a rule, not a proof.
- **Contamination.** Public tasks contain reference solutions. Nothing is public before release.
- **Licenses.** A benchmark that forbids redistribution should not go into a public store. Nothing checks that yet.

## 8. Identity, local files, and what is uploaded

### Identity

`hm init` asks for a name, an affiliation, an email and a GitHub handle, and creates a signing key. Everyone runs it first. A person is identified by handle and key, never by name. The name, affiliation and email are listed in every index the person contributes to.

### What a run leaves on disk

```
~/.config/harbor-master/          identity, signing key, the theme
<workspace>/                      a clone of the index's repository
  about.toml  protocol.toml  contributors.toml
  benchmarks.toml  index.toml     written from the records
  skills/  adapters/<benchmark>/
  reports/<benchmark>/<task>.<digest>.md
  ledger/<benchmark>/<you>-<run id>.jsonl
  .hm/                            this machine only. Git ignores it
    hm.toml  owner  run.active
    sources/  adapted/  store/  simulated/  unkept.txt
    runs/<run id>/                run.json, tasks.jsonl, trials/, session.log
```

Nothing is stored twice. Harbor's output is kept only when it is evidence: an agent trial, or a run that went wrong. A simulated benchmark of 1,000 tasks leaves 64 KB in the run folder and 1.5 MB of ledger.

### API keys

Keys never enter a settings file, the ledger, or anything uploaded. Each clone of an index has its own `secrets.env` at its top, made by `hm clone` from the `secrets.env.example` the index commits, readable only by its owner and ignored by git, to fill in by hand. Environment variables come first. Outside an index (`hm new`) keys come from the environment only. Claude and OpenAI each take either an API key or a subscription sign-in, and one is enough: the curation agents and the agent trials both use whichever is set, the subscription where both are. A Claude subscription is a long-lived token from `claude setup-token`; a ChatGPT subscription has no such token, and is the `~/.codex/auth.json` that `codex login` writes, which Codex refreshes itself, found without any setting. An OpenAI key is logged in to Codex inside a throwaway Codex home, so a person's own `~/.codex` is never touched. Each person uses their own keys, including their own `HF_TOKEN`: the task store is a dataset in a Hugging Face organization, and its owner gives each contributor's account the write role there.

**Access is tried before a run starts** (`core/access.py`). Present is not enough: a key can be wrong or expired, a token can see the store without writing to it, a clone may not push, Docker may be down. So each is tried once, side by side, with the smallest real request: OpenRouter, Anthropic and OpenAI keys read an account or list models; a Claude sign-in, which has no free check, makes one call of a few tokens; the store is asked whether this token may write; the repository gets a dry-run push to a branch nobody uses; Docker must answer `docker info`. Every problem is listed together, and nothing is claimed or spent. A claim whose push never succeeds is not taken: the benchmark is skipped with that reason, so two people never curate it at once.

**A hung trial is stopped, not waited on** (`backends/harbor.py`, `Watch`). Every few minutes (`watch_minutes` in `hm.toml`, 3) a running trial's folder and Harbor's output are looked at. Once nothing has been written for `quiet_minutes` (9), the worker agent reads the last lines of its logs (`skills/audit/stuck.md`) and answers hung or working. Working, it is looked at again after another quiet spell. Hung, Harbor is interrupted, every container of the trial is removed (Harbor names each trial's Compose project after the trial), and a box asks the person to retry the trial or drop the task. Its judgement and the person's choice are both recorded. The reference solution's runs, the no-op, the wrong answers and every agent trial are watched; the adapter's check runs are not, and keep their time limits.

### Where each thing goes

| What | Goes to |
|---|---|
| The index's definition, adapters, ledger, reports | The index's repository |
| The accepted tasks | The task store |
| Trial archives, mutants, run summaries, statistics | Nowhere: `.hm/` on the machine that made them |
| Identity and keys | Nowhere |

### The task store

A later benchmark run must run exactly the task that was accepted. Tasks are too large for git (the largest of Terminal-Bench-Science is 196 MB), a repaired task exists only where it was repaired, and a source can disappear. So each accepted task is kept as `tasks/<benchmark>/<task>.<digest>.tar.gz`.

- **Every index has one**, named in `about.toml`: a Hugging Face dataset repository, `org/name`. `hm new --tasks` names it and creates it, private. A folder's path also names a store, which the tests use. Only `demo-index`, which comes with the tool, has none.
- **Each person uses their own token**, `HF_TOKEN`, with write access that the owner grants on Hugging Face. Reading a public store needs no token.
- **A real run checks it first**: the store is named, exists, and the token is there. Whether the token may write shows at the first upload.
- **Accepting a task uploads it.** A failed upload is reported and noted on the machine, and its next `hm` command tries again.

### Reports

A reviewer signs from the report, so it is written into the index's repository and committed with the run's records. Its record holds a checksum, and a report changed afterwards is reported.

### What is not shared, and what that gives up

Trial archives stay on the machine that made them. The ledger holds each trial's result, so nothing the system does needs them elsewhere. What is given up: nobody else can open a trajectory to check a trial.

## 9. Config

### The index repository

Each index is a git repository of its own: its definition and its ledger. A clone is the workspace.

| File | Says | Who changes it | How it is protected |
|---|---|---|---|
| `about.toml` | Name, description, the task store | The owner, shared by their next `hm` command | Changes no result |
| `protocol.toml`, `skills/` | Every setting and wording that can change a result | The owner, shared by their next `hm` command | Every record carries their hash |
| `contributors.toml` | Who may contribute, and their keys | The owner, with `hm contributors add` | The owner signs each change |
| `benchmarks.toml` | The list of benchmarks | Nobody by hand | Written from signed records |
| `adapters/<benchmark>/` | Code that writes Harbor tasks from another format | The run that first takes the benchmark | A signed record that this version passed |
| `ledger/`, `reports/`, `index.toml` | The records, the reports, the accepted tasks | `hm` | Signed, chained, checksummed |

- **Whom a clone trusts.** Everything rests on the owner's signature. A workspace remembers the owner's handle and key from the day it joined (`.hm/owner`) and refuses a copy that names another.
- **What a run commits.** Its records, its reports, and `benchmarks.toml`. The definition is committed only by the owner's commands.
- **Skills.** `hm new` copies the templates into the index. An index may reword a skill freely but not change its structure, and a copy the code cannot use is refused. A run refuses an index that lacks a copy of a skill.
- **The tool's version.** `[requires] harbor_master` is the oldest tool that may run the index.

**The list of indexes.** `indexes.toml` in the Harbor-Master repository holds one entry per index: name, address, the owner's handle and key. It is read from the main branch, so a new entry needs no release. Everything else about an index is read from its own repository. Getting listed is `hm publish`, owner only: it checks the index as the list's check would (the canary, a name, a description, a task store, an owner with a key, everything pushed, the skills, the ledger, and a clone made as anyone would make it), then asks, and opens a pull request that adds the entry, through the GitHub CLI: a fork, a branch from the main branch, one commit. Without the CLI it prints the entry and the link to add it by hand. The pull request is checked by `hm indexes --check`. `hm clone NAME` checks the clone against the owner's key on the entry.

**`demo-index`** comes with the tool. `hm clone demo-index` makes it from the tool's own templates, with a fixed canary, whoever made it as its owner, and three made-up contributors (a lead and two contributors) whose keys are made in its `.hm` folder. It is a git repository with no remote, names no task store and lists no benchmark, so it is for simulated runs. Before the demos, the made-up people curate three made-up benchmarks and leave a report to review, each signing as themselves: `hm curate --demo`, and `hm view --demo`, which shows anyone the dashboard over the demo's records and makes them first if there are none. `hm indexes` lists it first. A site built from this list is not built.

### The benchmark list

Anyone on the contributor list adds a benchmark, and the owner does nothing for it.

- **One list.** `/benchmarks add URL` ends in a signed `benchmark` record. `benchmarks.toml` is the list as one file, written from the records after every pull. Editing it changes nothing.
- **Checked before and after.** Adding pulls first and refuses a repository or an ID that is listed already, saying who added it. Confirming pulls and checks again. When two people add at once, the pull settles the file by itself.
- **A person gives a link, of any kind.** A worker-tier agent with web search finds the benchmark's repository from it (a repository in any form, a Hugging Face page, a paper, a project page or a name), its newest release, and drafts the name, description, paper, release date, licence and homepage, with the pages it read. Code then pins that release and its commit, downloads it, finds the tasks, and refuses a repository already on the list. The release date is what the benchmark-level recency check compares; where no source says, the pinned commit's date stands in. The person checks the draft once and adds it, or says what to change (`field: value`, or in words for the agent), and nothing is signed before that. In a simulated run a stand-in makes the benchmark up, with made-up tasks registered on this machine only.
- **Then cheap checks on the whole benchmark** (`tasks/checks.py`), before anything costly is spent on it: released after the cutoff; its tasks are for agents, not one typed reply; a program grades it; it has reference solutions; its tasks are public, not held out; it is not already solved (best reported score); its license allows evaluation; its container images are public. No trial is run and no model is asked: each reads the entry (what the agent found on the web), the files of the pinned release, or a registry. A benchmark is rejected when its tasks are not for agents (multiple choice, question answering, short answer) or not public (held out, or gated behind a request), or on hard evidence: no tests, no reference solution, every image refused, or a release date before the cutoff (shown in the draft, where it can be corrected). The rest only warns: grading as the agent read it, how far it is solved, its license. Neither the size of a benchmark nor leaked answers are checked. The curate screen asks them one by one in a list that scrolls inside a box. Passed, the person continues, and it is added and curated. Only a failure rejects: the box says why, and the person confirms, and it is not added; there is nothing to choose. Either way, back goes to the draft. A run that takes a listed benchmark asks the same checks before its images and adapter, once per pinned release, so an old benchmark or one of quiz questions never costs an adapter: a failure there is confirmed in a box, and the benchmark is dropped.
- **The pin stays.** Only the adder or the owner may replace an entry or take it off.
- **Why it is safe to leave open.** Adding spends nothing and admits nothing.

### Adapters

A benchmark whose tasks are not Harbor task folders needs an adapter. It has Harbor's own adapter layout, so one built here can be offered to Harbor.

- **A run builds it by itself.** The worker model writes it, the checks run, and a failing one goes back with its failures (`adapt.frontier_after_round` rounds, 2).
- **Jev decides what happens next.** When the worker's rounds fail, the decision model, Jev, is asked whether the benchmark can be adapted at all (`skills/adapt/judge.toml`), and answers with a typed choice and its probability, recorded as a signed `decision` on that version of the adapter. If it can be adapted, or Jev declines to answer, the frontier model tries (`max_rounds` minus those, 1 round), with nobody asked. If it cannot, a box shows Jev's judgment, what failed and the worker's notes: the person agrees, and the benchmark is dropped, or types a hint, which goes to the frontier model with the failures. With nobody to ask, the benchmark is left for a person, not dropped.
- **If the frontier model fails too,** the benchmark is dropped at once, with a signed `decision` that says why, and a box tells the person, who acknowledges it; the run goes on with the others.
- **It converts, it does not curate.** The adapter writes every task once, cheaply, so the screen and the ranking can read them. It is told Harbor's own adapter guide (its sections 1 to 3, read from the installed Harbor), keeps each task's instruction, check and solution, mends what is broken for every task (a wrong path, a missing dependency) and lists each fix, and writes a task whose solution is missing with an empty solution folder. Making each task valid is left to the work cells, after the selection: only the selected tasks cost a repair.
- **The checks need no model:** the layout is complete, it runs, every task is complete, a second run writes the same tasks by digest, and the reference solutions of 3 tasks pass through Harbor (`adapt.oracle_tasks`).
- **A signed record vouches for it.** A run uses an adapter only when someone on the list signed that this very version passed. A changed adapter is checked again. It needs no lock.
- **Its code runs in a container** with no network and read-only mounts.
- **A dropped benchmark may be curated again.** It stays on the list, marked dropped, with who dropped it and why. Anyone may take it again from the benchmark box, as a listed one; once a run claims it, it is no longer dropped, and if that run drops it too, it is dropped again. A link to it is not added again: it points to the entry on the list.
- **Not built:** parity against the original harness, and adapters that need more than the standard library.

### The two settings files

| File | Holds | Who changes it |
|---|---|---|
| `protocol.toml`, in the index | Tiers and models, gates, thresholds, the difficulty suite | The owner, shared by their next `hm` command |
| `hm.toml`, in each clone, git-ignored | Cells, containers, claims, sandbox, the hung-trial watch | Whoever runs there |

Both are written commented by `hm new`. The index commits `hm.example.toml` and `secrets.env.example`, and each clone's own `hm.toml` and `secrets.env` are made from them by `hm clone` (and by any `hm` command in a clone that lacks them); git ignores both. The defaults are in `templates/protocol.toml` and `templates/hm.toml`.

| Setting | Default |
|---|---|
| `escalation.min_confidence` | 0.85 |
| `triage.released_after`, `max_verifier_seconds`, `max_build_seconds`, `max_reported_pass_rate` | 2025-06-01, 300, 300, 0.4 |
| `select.top_k`, `max_fraction`, `comparisons_per_task` | 24, 0.25, 12 |
| `difficulty.suite` | Fable on Claude Code, GPT-6 Astra on Codex, both pinned |
| `difficulty.max_pass_rate`, `trials`, `max_agent_hours` | 0.50, 3, 4 |
| `difficulty.cheap_solve`, `cheap_trials`, `cheap_agent`, `cheap_agent_hours` | true, 2, claude-code, 1 |
| `adapt.max_rounds`, `fix.max_rounds`, `fix.frontier_after_round` | 3, 2, 1 |
| `run.cells`, `max_containers`, `claim_hours` | 4, 16, 12 |

### Settings and the version

There is no lock. What an index is made of is its owner's files, shared through its repository, and every record says which settings it was made under.

- **The hash** covers every setting in `protocol.toml` except `[requires]`, and every curation skill. Every record carries it, so results made under different settings are never mixed up, even though nothing refuses a run.
- **The version** is `version` in `about.toml`, which the owner sets: raise it when a change to `protocol.toml` or `skills/` changes results. It starts at 0.1.0, and becomes 1.0.0 when the owner says the index is finished.
- **Sharing, with no command for it.** Every command inside an index starts the same way (`cli/sharing.py`): it pulls what others pushed; for the owner, it commits and pushes their edits to `about.toml`, `protocol.toml`, `skills/` and `README.md`, and signs the contributor list if they edited it by hand; it checks the ledger and, if the ledger is sound, pushes any records of this machine's that were not pushed; it creates the task store `about.toml` names if it is not there yet (the owner's command), and puts accepted tasks that did not reach the store there. It prints nothing unless something needs saying. Everyone else gets the owner's edits at their next command.
- **Before every run** the tool's version must be at least `[requires] harbor_master`, every skill must be there and readable, and the contributor list must be the one the owner signed.
- **The contributor list** is signed by the owner at each `hm contributors add`, `role` or `remove`, and a copy that differs from the signed one is refused.

## 10. CLI

Python, installed as `hm` with `uv tool install git+https://github.com/Open-SSI/harbor-master` (or `--editable .` from a checkout). Typer defines the commands, Textual draws the screens, Rich prints.

**A shell for managing, and two screens for contributing.**

| Where | Commands | For |
|---|---|---|
| Any folder | `hm init`, `hm indexes`, `hm clone INDEX`, `hm new INDEX --tasks ORG/NAME --repo OWNER/NAME` | Everyone. `hm new` makes its caller the owner |
| Inside an index's folder | `hm curate`, `hm benchmarks [add, revise, confirm, remove]` | Everyone |
| Inside an index's folder | `hm contributors [add, role, remove]`, `hm publish` | The owner |
| Inside an index's folder | `hm view` (the dashboard), `hm cut TASK why`, `hm restore TASK` | The owner and the leads |

- **`hm` alone is an error** that lists these. A command about an index typed outside one is refused, with a line that says to `cd` into the index's folder.
- **Only `hm init` asks in boxes** (`tui/wizard.py`): one question per box, a line to type in or a button for each choice, Cancel and Next at the right. It is the whole onboarding, the same for everyone, and run again it is how a person changes their details: each box shows what the machine has, to keep with enter or type over, and the signing key is kept. It asks who you are and makes the signing key, and its last box only says how to start: `hm clone demo-index`, then `hm curate --demo` and `hm view --demo`. It asks for no keys, since nobody is in an index yet: the keys file is made, and the keys asked for, when a real run needs them. A machine runs 4 tasks and 16 containers at once unless its own settings say otherwise. `hm new` and `hm clone` are plain commands.
- **The keys are checked before a run.** `hm curate` makes the keys file if there is none, lists every key a real run needs that the keys file lacks (each model tier, each agent of the difficulty suite, and a Hugging Face token when the store is there), and in a terminal waits while they are added, checking again at each enter.
- **`hm clone INDEX` clones.** It takes a name from the list of indexes or a repository's address, clones it into a folder here, and refuses it unless it is an index: a `protocol.toml`, and an `about.toml` with the canary line (`harbor-master-canary GUID ...`, written by `hm new`). A refused clone is removed. `hm clone demo-index` makes the demo index instead.
- **What an index is made of is its owner's files.** `about.toml`, `protocol.toml` and `skills/` are edited in the repository. The owner's next `hm` command shares them. No screen changes them, and no screen shows the protocol: its hash goes on every record by itself.
- **There are no other commands.** Eleven, all in `hm --help`, none hidden. What the old ones did is done by the run itself (fetching a benchmark, writing its adapter, putting an accepted task in the store), by the start of every command (pulling, checking the ledger, pushing, sharing the owner's files, creating the task store, retrying a task that did not reach the store), or shown on the dashboard.

`hm curate` also takes `--demo`, `--tasks`, `--until select|verify|probe`, `--assume-after`, `--plain` and `--no-input`.

### The dashboard

![The index's dashboard](assets/dashboard.png)

`hm view`, inside an index's folder, shows its owner and its leads where the index stands. Everyone else is told to use `hm curate`. `hm view --demo` is anyone's: it shows the simulated demo's records.

- **Read only.** Nothing is started from it and nothing is changed. It has no line to type in.
- **The header** is the one every screen has: the tool, the index, the person and their role, "curating" and the version.
- **Six figures**: benchmarks, how many are curated, tasks accepted, reports to review, contributors, spent.
- **Panels**: curation, a row per benchmark with its status (listed, curating by whom, curated, dropped), a bar, and its accepted, to review, rejected and dropped tasks (nobody's work in progress, which is not shared); the contributors, with what each signed, ran and spent.
- **Buttons** open the details in a box to read and scroll: Status, Tasks, Benchmarks, Credits, Skills. They are functions, not commands (`cli/views.py`); `hm benchmarks` prints the same Benchmarks.
- **Figures are read from the records** (`tui/home.py`, `gather`), again every few seconds, so a run that is going moves them. While an index has only simulated runs, their figures are shown, and the header says so.
- **The curate screen has none of this.** It shows its own run and nothing else, so no feature is in two places.

**What a simulation is.** One backend stands in for all three model tiers and for Harbor. Each task gets hidden properties drawn from its digest (is it hard, is its verifier broken, can it be repaired), and every answer is one more draw from that digest, so a run can be repeated exactly. Nothing is computed for long: a 40-task run takes a fraction of a second. The only time it takes is a pause: two seconds for each pretend agent session or container run, and ten or more for each agent's attempt at a task, so that the difficulty run is visibly the long stage. `--speed 0` removes the pauses. A simulated run records a walk-through: the first time it reaches a stage, it notes what the stage does. It sends every task that passes the gates through the stages, up to `top_k`. `hm curate --demo`, in `demo-index`, adds one benchmark of 10 made-up tasks to it, on this machine only, and starts a simulated run of it, which takes two to three minutes.

**A simulated run never counts as real.** Its records go to `.hm/simulated/`, never to `ledger/`. Each carries `simulated: true` inside what is signed. A real ledger leaves such a record out, and the ledger check before every push reports one. The dashboard shows simulated records while an index has nothing else, and says so.

### A run outlives its terminal

```
terminal  <->  view (hm run)  <- socket ->  relay  <- pseudo-terminal ->  the run and its screen
```

- **Leaving.** `ctrl+l`, closing the window, or a lost connection closes the view. The run goes on.
- **Coming back.** `hm curate` inside the index's folder, from any terminal, opens a view on the run that is going.
- **One run per index.** Two indexes run side by side, each behind its own relay.
- **A finished run stays**, for verdicts or another benchmark, which its first box offers again, until `ctrl+q`.
- **Ending cleanly.** A run told to end releases its claims, writes its usage, and pushes.
- **It does not survive a reboot.** The next run reuses everything that was recorded.

### The curate screen

![The curate screen in the curating stage](assets/curate.png)

- **It starts with a box:** which benchmark (`tui/boxes.py`, `PickScreen`). It lists the index's benchmarks with their status, and takes the name of one that is listed, or a link to a new one: the worker agent searches the web for it while that box says so, and the next box opens with what it found and the code downloaded, to check once; corrections are typed as `field: value` or in plain words, and adding it signs it and curates it (`tasks/adding.py`, shared with `hm benchmarks add`). The run starts empty and waits for it (`Session.run`), and when everything is finished the same box opens for the next benchmark.
- **The benchmark's stages** are boxes joined by arrows (`tui/stages.py`): fetch, adapt, screen, rank, select, curate, review, curated. The one it is in is lit and breathes, the ones behind are ticked, the ones ahead faint, one that is not needed (adapt, for Harbor tasks) is struck, and one where it stopped is red. Under each box a count: tasks, kept, ranked, chosen, working, to review. A line under them says what the stage does, and where it is.
- **The curating stage** starts with a box, once the tasks are chosen: how many work cells (1 to 8, `MAX_CELLS`) and containers run at once (`CellsBox`). Both start from `hm.toml`. Only a run with a screen asks; one with none uses those settings.
- **One work cell is drawn at a time,** under the benchmark: a tab per cell (its task and stage), then its task's stages the same way, verify, probe, worker, audit, fix, difficulty, report, with an arrow back from fix to verify that lights while a task is repaired. Difficulty sits after the loop. Keys `1` to `8` choose the cell; `enter` opens its task.
- **When it is done,** the benchmark is released as curated, and the first box comes back for another. On a short terminal, what happened last, the figures and the header give way first.
- **Teal** is done or kept, **purple** running, **amber** needs attention or a person, **red** stopped or dropped.

**Where a benchmark stands** (`ledger/benchmarks.py`, `statuses`): *dropped* when the latest signed drop record (rejected, or no task accepted) is newer than the latest claim on it; *curating* while a claim on it holds, with who and until when; *curated* when the last claim was released by a run that finished every task it chose (the release record says `curated`); else *listed*: never started, let go when its run ended, or after `claim_hours` (12) with no progress. `benchmarks.toml` carries the status, written from the records on every commit like the rest of it.

### Boxes, not commands

**No screen has a prompt.** A person cannot type a command, so nothing they do can upset a run. What they decide, they decide in a box that the run opens and that has to be answered before anything else:

| Box | Opens when | The choices |
|---|---|---|
| Which benchmark | The curate screen opens | One that is listed, or a link to add one |
| Work cells and containers | A benchmark's tasks are chosen | 1 to 8 cells, and containers |
| A brief | A benchmark has passed its gates | A sentence or two, typed |
| The selection | The tasks are ranked | How many go on, typed |
| A report | A kept task reaches its report | `accept [why]` or `reject <why>`, typed under it |
| A new benchmark | A link is pasted | Its draft, checked once, then its checks: Continue (or Confirm a rejection), or Back |
| A benchmark rejected | Its checks, its adapter, its image check or its screen reject it | Confirm: it says why |
| A trial is stuck | A trial's logs stood still and the worker agent read them as hung; the trial is already stopped | Retry it, or drop the task. It says what the trial was doing and why it is judged hung |
| Ending | `ctrl+q` | Keep it going, or end the run |

The choice boxes are one widget (`tui/parts.py`, `ChoiceBox`), used by all three screens. The keys are few: a cell's number opens its task, `pgup`/`pgdn` scroll the log, `ctrl+l` leaves, `ctrl+t` switches light and dark, `ctrl+q` ends. There is no pause: to stop, end the run, and its claims are let go. The commands behind the boxes are still in `interact/`, where the boxes and the relay run them. A trial that fails for an infrastructure reason is tried once more by itself, with no box. With nobody to ask (`--no-input`, `--plain`), a stuck trial is run again once, and then its task is dropped.

### When the system asks the user

Each need becomes a request, and each request opens its box. Only the work that depends on the answer waits.

| Request | Raised when | Blocks |
|---|---|---|
| Brief | A benchmark has no brief | Its ranking |
| Selection | The ranking is done and not approved | Its costly stages |
| Adapter | The models could not make an adapter pass | That benchmark |
| Budget | Nine tenths of the limit is spent, and again when it is gone | Nothing at first, then new tasks |
| Stuck trial | A running trial is judged hung | That task's work cell |
| Verdict | Reports are ready | Nothing |

With `--no-input` nothing prompts: dependent work is skipped, the rest runs, and the exit code says requests remain.

## 11. What a run reports

Everything a paper needs is computed from the ledger. Two records exist for it: `usage` (calls, tokens and cost per benchmark, stage and tier) and `skip` (a benchmark a run left out, and why).

All of it can be computed from the ledger, which records every model call, decision and trial. No command writes it as a report yet.

## 12. Estimated curation cost

Modeled, not measured. Per 1,000 tasks entering, with no selection cap, without the frontier difficulty trials that every approach must run. Assumes $0.30 per Sonnet session, $1.50 per Opus call, $50 per reviewer hour.

| | Harbor-Index | Ours | Ours, one Opus does everything |
|---|---|---|---|
| Agent cost | ~$15 | ~$525 | ~$2,700 |
| Human hours | ~130 | ~15 | ~15 |
| Total | ~$6,500 | ~$1,300 | ~$3,450 |

Ours spends about $500 more on agents and saves about 100 expert hours. The verifier probe is its largest line (~$260). With the selection cap, at most 24 tasks of a benchmark reach the costly stages, so the cost no longer grows with a benchmark's size.

## 13. Implementation status

As of 2026-11-19. The package is `src/harbor_master`. 82 tests, on simulated backends and local git repositories.

```
src/harbor_master/
  cli/         the hm commands, one module per group
  tui/         the index's screen and the run screen, colors
  interact/    what a user can type, and the guide
  skills/      what each agent is told
  engine/      a run: session, shared (per benchmark), prepare (fetch and adapt), stages (per task)
  backends/    who decides and who runs trials: real and simulated
  curation/    the rules as pure functions
  ledger/      records, the fold into state, verification, sharing, statistics
  tasks/       the Task model, the benchmark list, adapters, the task store
  core/        config, identity, keys
  templates/   the files a new index starts from
  host.py      the relay that keeps a run alive
```

| To add | Change |
|---|---|
| A stage | One function in `engine/stages.py` |
| An admission gate | Its wording in `skills/triage/gates.toml`, its rule in `curation/gates.py` |
| A session command | One decorated function in `interact/handlers.py` |
| An `hm` command | One function in the matching `cli/` module |
| A record type | `ledger/events.py`, and its rule in `ledger/fold.py` |

**Built and tested in simulation:** the whole pipeline, the ledger and its checks, both screens, the index as a repository, sharing with claims, the benchmark list, adapters, the task store behind a folder, statistics, the run that outlives its terminal.

**Tried for real, on Terminal-Bench-Science tasks:**

- Harbor in Docker for the reference solution, a no-op and a mutant.
- The gates and the ranking on Jev: 40 gate decisions and the comparisons for under $0.001. Escalations to Sonnet cost about 200 times more than everything Jev did.
- A hand-written adapter in the container, with a reference solution passing through Harbor.
- `hm clone INDEX`, the listing check and `hm publish` against GitHub.

**Never run for real:**

- A model writing an adapter, or drafting a benchmark's entry.
- Any tier on the `codex` provider.
- Agent trials for the difficulty filter, the worker model's first tries, and the audit, repair and report on real trajectories.
- The agent that reads a benchmark's per-task results, the coverage question on a real model, and trivial submissions through Harbor. The trivial submissions are a heuristic, tested on made-up tasks only.
- The task store against Hugging Face.

**Not built:** parity runs, the exploit agent, recalibrating thresholds, the site of indexes, and the session commands `/logs`, `/trajectory`, `/restart`, `/retry`, `/escalate`, `/limit`. Task digests still use a stand-in hash, not Harbor's.

## 14. Open

- Jev in practice: input length, latency under load, and whether its probabilities are calibrated on these gates.
- Parity when the original harness cannot run.
- A benchmark whose licence forbids redistribution, and the task store.
