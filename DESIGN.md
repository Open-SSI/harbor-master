# Harbor-Master design

Harbor-Master takes a task or a benchmark in any format and returns a decision a person can sign: accept into an index, or drop, with the evidence. It holds no index of its own. Each index is a repository that says what it curates and how (Section 10). Section 14 says what is built, and Section 15 what is open.

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

A stage calls the next tier up only on a recorded trigger: low confidence, disagreement between two checks, or a retry limit. The share of tasks that reach tier F is reported for every run. The frontier models that are the *subjects* of the difficulty filter are a separate budget.

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
| 1 | Triage | D, then W when unsure | The admission gates below | A gate fails |
| 2 | Adapt | W, then F, then a person. Once per benchmark, skipped for Harbor-format input | An adapter with a signed record that it passed, and the tasks it writes (Section 10) | The person abandons the benchmark |
| 3 | Verify | Code | Reference solution passes twice, a no-op fails, the verifier is fast enough | Still failing after the retry limit |
| 4 | Probe | W and code | Mutants of the solution test the verifier before any frontier trial | The verifier is wrong and repair fails |
| 5 | Difficulty | Evaluation suite | 2 frontier agents x 3 trials, in rounds, each at most 4 hours | More than 40% pass |
| 6 | Audit | W; F when unsure and on a sample | Reads trajectories against the rubric, and says whether a failure is the task's, the agent's, or the infrastructure's | Broken and not repairable |
| 7 | Fix | W; F after two rounds | Repairs the task and returns it to stage 3 | Round limit reached |
| 8 | Report | W | One report per task: evidence, diffs, cost, recommendation | |
| 9 | Verdict | A person | Accept, reject, or send back, signed and credited | |

**Admission gates.** A task is kept only if all hold. Each failure has its own reason code, and the gates apply per task.

| Gate | Keeps | Settled by |
|---|---|---|
| Format | Tasks where an agent executes work, not multiple choice or short answers | D |
| Reference solution | A runnable one | Code, then stage 3 |
| Verifier | Programmatic and deterministic, not judged by a model or a person | D, then stage 3 |
| Verifier cost | Finishes within `max_verifier_seconds` on CPU, with no paid API | D estimates, stage 3 measures |
| Recency | The benchmark's release date is after the cutoff | Code |
| Duplicate | Not in the ledger already | Code, D for near matches |
| License | Allows use for evaluation | D |

**Ranking.** The rank comes from comparisons, because scores on a short scale tie.

1. *The lead's brief*: a few sentences on what matters in this benchmark, stored as a signed `brief` record.
2. *A score for every task*: tier D reads the brief and a short card of the task. One request per task.
3. *Pairs near the top*: the best `pool_factor` times the cap are compared in pairs, each pair in both orders, about 12 per task, and a Bradley-Terry model turns the results into a rating.
4. *The lead approves the selection* before any trial is spent, or gives a new brief, which ranks again.

Measured on the 70 tasks of Terminal-Bench-Science: 468 requests, $0.023, 11 seconds. The pool depends on the cap, not on the benchmark's size.

**Selection cap.** Only the top `min(top_k, max_fraction x N)` tasks of a benchmark go on to the costly stages: 24 of 1,000, 18 of 70. Selection goes round-robin over sub-areas. The rest are `not_selected`, not dropped.

**Early stop** in stage 5: with 6 trials and a 40% threshold, a task is dropped once 3 trials pass and kept once 4 fail.

**The index cap.** A benchmark may have at most `max_per_benchmark` tasks in the index, 8 by default. The cap applies at the verdict: accepting a ninth asks which task it replaces, and signs both verdicts together.

## 5. Where this goes beyond the paper

1. **Verifier probes before frontier trials.** A worker writes mutants of the reference solution: some wrong in a way that matters, some right in another form. The verifier must reject the first and accept the second.
2. **Calibrated escalation.** Every decision is stored with its probability, and every later overturn is a label. Thresholds can be refit on those labels.
3. **Sequential difficulty filter**, with early stop and reuse of any valid trial in the ledger.
4. **Content-addressed record.** A repair changes a digest, so only what depends on it runs again.
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
| `protocol` | The owner locks the settings, with a version |
| `submission` | A run takes in a benchmark's tasks, with their digests |
| `decision` | Any gate, the ranking, an adapter that passed or was abandoned |
| `trial` | Any run of an agent or the verifier on a task: purpose, agent and version, model, pass, tokens, cost |
| `repair`, `report`, `verdict` | The fix loop, the end of curation, a person's judgement |
| `claim` | Before the costly stages, and again when the run ends |
| `brief` | A lead says what matters |
| `usage`, `skip` | What the tiers were called for, and a benchmark a run left out (Section 12) |

A difficulty trial and a later benchmark trial are the same `trial` record with another `purpose`, so the difficulty filter and the leaderboard read one table.

### Skills: what each agent is told

Every prompt and criterion is a file, grouped by stage. Code holds the rules, skills hold the wording. The curation skills (`triage/`, `rank/`, `adapt/`, `probe/`, `audit/`) decide what gets into an index, so each index keeps its own copies under `skills/`, and they are part of the protocol hash. The session skills (`session/`: reading a typed line, answering a question, drafting a benchmark's entry) change no result and stay in the tool.

A skill's version is a hash of its file, and every record made with one names it, for example `triage/gates@dee91cd340`. So two wordings can be compared on the same tasks by their overturn rates and cost.

### Credit

Computed from the ledger: per person, tokens and dollars contributed, trials run, benchmarks added, repairs, verdicts. `hm credits` prints it.

### Never run twice

A trial slot is (task digest, agent and version, model, effort, protocol, trial index). A run refuses a filled slot and shows the existing result. Decisions and verdicts work the same way. Agent versions are pinned: each trial records the version the agent itself reported, and `hm ledger verify` rejects a trial whose two versions differ.

### Two people at once

- **Claims.** Before the costly stages a run pulls, writes a `claim` naming its tasks with an expiry (`claim_hours`), and pushes.
- **The push is the lock.** Git refuses a push when someone pushed first. The refused run pulls, sees the other claim, and gives way on what both claimed.
- **Screening** a benchmark is held by one person at a time, the same way.
- **A rerun is refused.** A task that has an outcome under this protocol is not run again. One that failed or was stopped has none, so it is free for anyone.

### Concurrent writers

Each run appends to its own file, `ledger/<benchmark>/<runner>-<run id>.jsonl`, with its own hash chain, so git never sees a conflict. A task's state is a fold over its records: for automated decisions the higher tier wins, and human verdicts are never overwritten.

### What is not solved by this

- **Trust.** A contributor could submit trials that never ran. Records are accepted only from listed people, signed. That is a rule, not a proof.
- **Contamination.** Public tasks contain reference solutions. Nothing is public before release.
- **Licenses.** A benchmark that forbids redistribution should not go into a public store. Nothing checks that yet.

## 8. Identity, local files, and what is uploaded

### Identity

`/init` asks for a name, an affiliation, an email and a GitHub handle, and creates a signing key. A person is identified by handle and key, never by name. The email is kept private.

### What a run leaves on disk

```
~/.config/harbor-master/          identity, signing key, the theme
<workspace>/                      a clone of the index's repository
  about.toml  protocol.toml  roster.toml  contributors.toml
  benchmarks.toml  index.toml     written from the records
  skills/  adapters/<benchmark>/
  reports/<benchmark>/<task>.<digest>.md
  ledger/<benchmark>/<you>-<run id>.jsonl
  .hm/                            this machine only. Git ignores it
    hm.toml  owner  run.active
    sources/  adapted/  store/  stats/  simulated/
    runs/<run id>/                run.json, tasks.jsonl, trials/, session.log
```

Nothing is stored twice. Harbor's output is kept only when it is evidence: an agent trial, or a run that went wrong. A simulated benchmark of 1,000 tasks leaves 64 KB in the run folder and 1.5 MB of ledger.

### API keys

Keys never enter a settings file, the ledger, or anything uploaded. `/keys` creates `secrets.env`, readable only by its owner and ignored by git, to fill in by hand. Environment variables come first. Claude and Codex each take an API key or a subscription sign-in. Each person uses their own keys, and `/doctor` shows which are found.

### Where each thing goes

| What | Goes to |
|---|---|
| The index's definition, adapters, ledger, reports | The index's repository |
| The accepted tasks | The task store |
| Trial archives, mutants, run summaries, statistics | Nowhere: `.hm/` on the machine that made them |
| Identity and keys | Nowhere |

### The task store

A later benchmark run must run exactly the task that was accepted. Tasks are too large for git (the largest of Terminal-Bench-Science is 196 MB), a repaired task exists only where it was repaired, and a source can disappear. So each accepted task is kept as `tasks/<benchmark>/<task>.<digest>.tar.gz`.

- **Every index has one**, named in `about.toml`: a Hugging Face dataset repository, `org/name`. `/new` asks for it and creates it, private. A folder's path also names a store, which the tests use. Only the index that `/demo` makes has none.
- **Each person uses their own token**, `HF_TOKEN`, with write access that the owner grants on Hugging Face. Reading a public store needs no token.
- **A real run checks it first**: the store is named, exists, and the token is there. Whether the token may write shows at the first upload.
- **Accepting a task uploads it.** A failed upload is reported, and `/tasks push` puts it there later. `/tasks pull` brings every accepted task onto a machine.
- **`hm eval` runs the accepted version or refuses.** A copy that does not match its digest is thrown away.

### Reports

A reviewer signs from the report, so it is written into the index's repository and committed with the run's records. Its record holds a checksum, and a report changed afterwards is reported.

### What is not shared, and what that gives up

Trial archives stay on the machine that made them. The ledger holds each trial's result, so nothing the system does needs them elsewhere. What is given up: nobody else can open a trajectory to check a trial.

## 9. Benchmarking on the index

The same tool runs agents on the finished index. A benchmark trial is made by the same function as a difficulty trial and leaves the same record.

- **The roster.** `roster.toml` lists the agent configurations to run on every accepted task. Roster times accepted tasks is every slot that should exist. `/todo` shows what is missing.
- **`hm eval`** runs a configuration on accepted tasks, each at its accepted version. Filled slots are refused.
- **The leaderboard** is computed from the ledger each time: a configuration's pass rate on each task, averaged over tasks. Infrastructure failures are left out. A configuration that has not run every task is shown as incomplete. The two agents of the difficulty suite are marked, since the tasks were selected against them.

## 10. Config

### The index repository

Each index is a git repository of its own: its definition and its ledger. A clone is the workspace.

| File | Says | Who changes it | How it is protected |
|---|---|---|---|
| `about.toml` | Name, description, the task store | The owner | Changes no result |
| `protocol.toml`, `skills/` | Every setting and wording that can change a result | The owner, by locking | The protocol hash is locked |
| `roster.toml` | Which agents the leaderboard compares | The owner, by locking | Its checksum is locked |
| `contributors.toml` | Who may contribute, and their keys | The owner, with `/contributors add` | The owner signs each change |
| `benchmarks.toml` | The list of benchmarks | Nobody by hand | Written from signed records |
| `adapters/<benchmark>/` | Code that writes Harbor tasks from another format | The run that first takes the benchmark | A signed record that this version passed |
| `ledger/`, `reports/`, `index.toml` | The records, the reports, the accepted tasks | `hm` | Signed, chained, checksummed |

- **Whom a clone trusts.** Everything rests on the owner's signature. A workspace remembers the owner's handle and key from the day it joined (`.hm/owner`) and refuses a copy that names another.
- **What a run commits.** Its records, its reports, and `benchmarks.toml`. The definition is committed only by the owner's commands.
- **Skills.** `/new` copies the templates into the index. An index may reword a skill freely but not change its structure, and a copy the code cannot use is refused. `/skills diff` and `/skills adopt` take in a newer template, as the owner chooses.
- **The tool's version.** `[requires] harbor_master` is the oldest tool that may run the index.

**The list of indexes.** `indexes.toml` in the Harbor-Master repository holds one entry per index: name, address, the owner's handle and key. It is read from the main branch, so a new entry needs no release. Everything else about an index is read from its own repository. Getting listed is a pull request with what `/entry` prints, checked by `hm indexes --check`. `/join NAME` checks the clone against the owner's key on the entry. A site built from this list is not built.

### The benchmark list

Anyone on the contributor list adds a benchmark, and the owner does nothing for it.

- **One list.** `/benchmarks add URL` ends in a signed `benchmark` record. `benchmarks.toml` is the list as one file, written from the records after every pull. Editing it changes nothing.
- **Checked before and after.** Adding pulls first and refuses a repository or an ID that is listed already, saying who added it. Confirming pulls and checks again. When two people add at once, the pull settles the file by itself.
- **A person gives an address.** Code pins the newest tag and its commit, finds the tasks, and reads the licence. A worker-tier agent reads the repository and drafts the name, description, paper and release date. The release date is what the recency gate compares; where the repository does not say, the pinned commit's date stands in and the draft says so. The person replies `y` or says what to change, and nothing is signed before the yes.
- **The pin stays.** Only the adder or the owner may replace an entry or take it off.
- **Why it is safe to leave open.** Adding spends nothing and admits nothing.

### Adapters

A benchmark whose tasks are not Harbor task folders needs an adapter. It has Harbor's own adapter layout, so one built here can be offered to Harbor, and one written for Harbor can be taken in with `/adapt ID --from PATH`.

- **A run builds it by itself.** The worker model writes it, the checks run, and a failing one goes back with its failures, to the frontier model after two rounds. A person is asked only when the models fail: fix it and `/recheck`, or `/abandon`.
- **The checks need no model:** the layout is complete, it runs, every task is complete, a second run writes the same tasks by digest, and a few reference solutions pass through Harbor.
- **A signed record vouches for it.** A run uses an adapter only when someone on the list signed that this very version passed. A changed adapter is checked again. It needs no lock.
- **Its code runs in a container** with no network and read-only mounts.
- **An abandoned benchmark is refused to every run**, with who abandoned it and why, until someone builds an adapter that passes with `/adapt ID`.
- **Not built:** parity against the original harness, and adapters that need more than the standard library.

### The two settings files

| File | Holds | Who changes it |
|---|---|---|
| `protocol.toml`, in the index | Tiers and models, gates, thresholds, the difficulty suite, the index cap | The owner, by locking |
| `.hm/hm.toml`, on each machine | Cells, containers, budget, sandbox | Whoever runs there |

Both are written commented by `/new`. The defaults are in `templates/protocol.toml` and `hm.example.toml`.

| Setting | Default |
|---|---|
| `escalation.min_confidence` | 0.85 |
| `triage.released_after`, `max_verifier_seconds` | 2025-06-01, 300 |
| `select.top_k`, `max_fraction`, `comparisons_per_task` | 24, 0.25, 12 |
| `difficulty.suite` | Fable on Claude Code, GPT-6 Astra on Codex, both pinned |
| `difficulty.trials_per_config`, `max_pass_rate`, `max_agent_hours` | 3, 0.40, 4 |
| `adapt.max_rounds`, `fix.max_rounds` | 3, 3 |
| `index.max_per_benchmark` | 8 |
| `run.cells`, `max_containers`, `budget_usd` | 4, 16, 200 |

### The protocol lock

A curation must run under one fixed set of settings, or its results cannot be compared.

- **The hash** covers every setting in `protocol.toml` except `[requires]`, and every curation skill.
- **Locking.** `/lock`, owner only, writes a signed `protocol` record with the hash, the settings, the roster's checksum and a version. Only what can change a result is locked. Benchmarks, adapters and contributors each have a signed record of their own.
- **Versions.** Every index starts at 0.0.0. A lock keeps the version unless the owner asks for a new one: `/lock patch` for a small change such as the roster (0.0.1 to 0.0.2), `/lock minor` for changed settings or skills (0.1.0 to 0.2.0). 1.0.0 is kept for the index's release and cannot be set yet. The hash on every record stays the exact identity of the settings.
- **Before every run** the local hash and roster are compared with the locked ones. A difference is refused, with what differs. A real run needs a lock. The owner is asked to lock right there.
- **The contributor list** is signed by the owner at each `/contributors add` or `remove`, and a copy that differs from the signed one is refused.
- **In the ledger.** `hm ledger verify` fails on a lock signed by anyone but the owner, and on records made under a hash that was never locked.

## 11. CLI

Python, installed as `hm` with `uv tool install --editable .`. Typer defines the commands, Textual draws the screens, Rich prints.

**Two screens, and a short shell.** A person works on the home screen and the run screen, where every step is a `/` command and a tip names the next one. `hm --help` lists only `run`, `status`, `stats`, `leaderboard`, `credits`, `todo` and `sync`. Every other command is hidden from that list and still answers by name, because the screens run them in place and scripts need them.

| Step | Commands on the home screen |
|---|---|
| Start | `/init`, `/indexes`, `/join`, `/new`, `/use`, `/demo` |
| Set up an index | `/index`, `/benchmarks [add URL, remove ID]`, `/roster`, `/skills [diff, adopt]`, `/protocol`, `/lock [patch, minor]`, `/connect`, `/entry`, `/contributors [add HANDLE KEY, remove HANDLE]`, `/whoami` |
| This machine | `/keys`, `/doctor` |
| Run | `/run [NAME] [--simulate]`, `/adapt`, `/claims` |
| Results | `/status`, `/leaderboard`, `/todo`, `/tasks [push, pull, store]`, `/stats`, `/credits`, `/sync`, `/verify` |
| Here | `/close`, `/theme [light, dark]`, `/help`, `/quit` |

`hm run` also takes `--tasks`, `--until select|verify|probe`, `--assume-after`, `--plain` and `--no-input`. `hm eval` benchmarks an agent on the accepted tasks.

### The home screen and the tips

![The home screen: a checklist of eight steps, a tip panel, and a prompt](assets/tui-home.png)

- **The checklist** has eight steps, each done, next, or later: who you are, an index, your place in it, the protocol, keys, a benchmark, a run, your verdicts.
- **The tip** is about the step marked next: each line is a command and what it does.
- **Forms.** `/init` asks four questions. `/new` asks five, of which the name and the task store are needed. A drafted benchmark waits for `y` or a reply.
- **Which step is next** follows what the person can do now. A made-up benchmark needs no lock and no keys.
- **One source.** Steps and tips are computed in `interact/guide.py` from what is on disk, for the home screen, `hm guide`, and the tip line of the run screen.
- **Light and dark.** `ctrl+t` or `/theme` switches both screens while they are open. The colors in the code are the dark ones, and in light mode a filter turns the lightness of every color over as the screen is drawn.

**A simulated run never counts as real.** Its records go to `.hm/simulated/`, never to `ledger/`. Each carries `simulated: true` inside what is signed. A real ledger leaves such a record out, and `hm ledger verify` reports one. The results commands take `--simulated` to show them.

### A run outlives its terminal

```
terminal  <->  view (hm run)  <- socket ->  relay  <- pseudo-terminal ->  the run and its screen
```

- **Leaving.** `ctrl+l`, `/leave`, or closing the window closes the view. The run goes on.
- **Coming back.** `hm run` or `/run` opens a view on the run that is going.
- **One run per index.** `hm run NAME` adds a benchmark to it. A finished run stays, for verdicts or more benchmarks, until `ctrl+q`.
- **Ending cleanly.** A run told to end releases its claims, writes its usage, and pushes.
- **It does not survive a reboot.** The next run reuses everything that was recorded.

### The session screen

![The session screen during a simulated run](assets/tui.png)

Header, summary, benchmarks, working cells, recent outcomes, and a prompt. The answer and confirm panels open only when needed. On a short terminal the cells stay and the other panels give way. Teal is done or kept, purple running, amber needs attention, red dropped. `1`-`9` opens a cell.

### Interrupting with plain text

The prompt takes a command (`/pause terrabench`) or ordinary language (`hold terrabench for now`). Typing never stops the run.

1. A `/` command is parsed directly, and completed with `tab`.
2. Anything else goes to tier D, which returns a typed intent: a question, a command with its target, or `unsupported`.
3. A command that only shows something runs at once. One that changes the run and was read from plain text is confirmed first. Under probability 0.5 the session asks what was meant.
4. Refusals are in code: the session will not change the protocol, edit or delete a record, mark a task accepted by hand, edit a task from the prompt, or run a shell command.

| Group | Commands |
|---|---|
| View, runs at once | `/status`, `/cost`, `/queue`, `/requests`, `/explain`, `/report`, `/review`, `/selection`, `/included`, `/model`, the results commands, `/theme`, `/help` |
| Control, confirmed when read from plain text | `/pause`, `/resume`, `/cells`, `/priority`, `/skip`, `/note`, `/add`, `/recheck`, `/undo`, `/leave`, `/quit` |
| Spends, discards or decides, always confirmed | `/stop`, `/kill`, `/drop`, `/budget`, `/brief`, `/approve`, `/abandon`, `/lock`, `/accept`, `/reject`, `/sendback`, `/replace` |

### Asking questions

A question is answered by one call of the chat tier with read-only records that code gathers: the task, its history, what the run is waiting on, time estimates. It starts no agent, uses no cell, and can change nothing. Reference solutions are never attached.

### When the system asks the user

Each need becomes a request: counted on the confirm panel, listed by `/requests`. Only the work that depends on the answer waits.

| Request | Raised when | Blocks |
|---|---|---|
| Brief | A benchmark has no brief | Its ranking |
| Selection | The ranking is done and not approved | Its costly stages |
| Adapter | The models could not make an adapter pass | That benchmark |
| Budget | The spending limit is reached | New tasks |
| Confirmation | A command would change, spend or decide | That command |
| Replace | A benchmark is full and one more is accepted | Nothing |
| Verdict | Reports are ready | Nothing |

With `--no-input` nothing prompts: dependent work is skipped, the rest runs, and the exit code says requests remain.

## 12. What a run reports

Everything a paper needs is computed from the ledger. Two records exist for it: `usage` (calls, tokens and cost per benchmark, stage and tier) and `skip` (a benchmark a run left out, and why).

`hm stats` writes `stats.md` and one CSV per table into `.hm/stats/`, which is never shared. It pulls first, covers everyone's records, and names the commit and checksum it read, so anyone can make the same report again.

| Section | Shows |
|---|---|
| 1. What happened to the tasks | The funnel from submitted to accepted, per benchmark, why tasks stopped, what each gate did |
| 2. What it cost | Calls, tokens and cost per tier and stage, runs through Harbor, cost per accepted task |
| 3. What people had to do | Decisions by a person, by kind. Benchmarks left out. Adapters built or given up |
| 4. Where the figures come from | The commit, the checksum, the protocols, the files |

Not recorded yet: how often a person was asked and how long the run waited, and the commands typed to steer a run.

## 13. Estimated curation cost

Modeled, not measured. Per 1,000 tasks entering, with no selection cap, without the frontier difficulty trials that every approach must run. Assumes $0.30 per Sonnet session, $1.50 per Opus call, $50 per reviewer hour.

| | Harbor-Index | Ours | Ours, one Opus does everything |
|---|---|---|---|
| Agent cost | ~$15 | ~$525 | ~$2,700 |
| Human hours | ~130 | ~15 | ~15 |
| Total | ~$6,500 | ~$1,300 | ~$3,450 |

Ours spends about $500 more on agents and saves about 100 expert hours. The verifier probe is its largest line (~$260). With the selection cap, at most 24 tasks of a benchmark reach the costly stages, so the cost no longer grows with a benchmark's size.

## 14. Implementation status

As of 2026-11-19. The package is `src/harbor_master`. 80 tests, on simulated backends and local git repositories.

```
src/harbor_master/
  cli/         the hm commands, one module per group
  tui/         the home screen and the run screen, colors
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
- `/join` and the listing check against GitHub.

**Never run for real:**

- A model writing an adapter, or drafting a benchmark's entry.
- Agent trials for the difficulty filter, and the audit, repair and report on real trajectories.
- The task store against Hugging Face.
- A benchmark trial through `hm eval`.

**Not built:** parity runs, the exploit agent, recalibrating thresholds, `hm contribute`, `hm publish`, acting on "send back", the site of indexes, and the session commands `/logs`, `/trajectory`, `/restart`, `/retry`, `/escalate`, `/limit`. Task digests still use a stand-in hash, not Harbor's.

## 15. Open

- Jev in practice: input length, latency under load, and whether its probabilities are calibrated on these gates.
- Parity when the original harness cannot run.
- A benchmark whose licence forbids redistribution, and the task store.
