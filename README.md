<h1 align="center">Harbor-Master</h1>

<p align="center">
  <strong>Harbor-Master</strong> curates benchmarks into an <strong>index</strong>: the tasks that are sound, verifiable, and still too hard for frontier agents. The first one is the <strong>OpenSSI Index</strong>, of scientific tasks. It takes a task or a benchmark, checks it in stages ordered by cost, repairs what is broken, and hands a human reviewer a report to sign.
</p>
<p align="center">
  Every decision, trial, and verdict is written to a <strong>signed ledger</strong>, so nothing is run or judged twice and anyone can check how a task got in.
</p>

<p align="center">
  <img src="assets/tui.png" width="100%" alt="The Harbor-Master session screen during a run: benchmarks with their progress, four working cells each carrying one task through the stages, recent outcomes, an answer to a typed question, and a confirmation before a command changes the run.">
</p>
<p align="center">
  <em>One run. Each working cell carries a task through the stages. The prompt takes commands, plain instructions, and questions, and asks before anything changes the run.</em>
</p>

> **Status.** The pipeline, the ledger, and both screens are built and tested on a **simulated backend**, which spends nothing. On real tasks, the admission gates and ranking (on Jev) and the verify stage (Harbor runs in Docker) work, and an adapter runs in its container. Not built: the audit, repair, and report on real trajectories. Never run: real agent trials for the difficulty filter, a model writing an adapter, and the Hugging Face task store. [DESIGN.md](DESIGN.md), Section 14, lists exactly what is and is not.

## How it works

Three model tiers share the work. A fast **decision** model makes every gate and routing choice. A **worker** model builds, audits, and repairs. A **frontier** model is called only when the first two are unsure or stuck. Cheap checks come first, so the expensive ones see few tasks.

For each benchmark, Harbor-Master:

1. **Fetches** it at its pinned release, and **adapts** it if its tasks are not in Harbor format.
2. **Admits** tasks that an agent must execute, that ship a reference solution, and that a program can verify quickly. Multiple choice and question answering are filtered out. An agent reads the results the benchmark itself reports, and a task that a model already solves more than half the time is dropped. So is a task whose answer is within the agent's reach.
3. **Ranks** the admitted tasks by importance under a brief from its lead: each is scored once, and the ones near the top are then compared in pairs. It **selects** the top ones: at most 24 per benchmark, and at most a quarter of it. Before any trial is spent, a page shows the ranked tasks, and you say how many of the top ones go on, or give a new brief to rank again.
4. **Verifies and probes** each selected task: the reference solution must pass, a no-op and an empty or placeholder output must fail, the verifier must reject wrong answers and accept right ones in another form, and the instruction must state everything the tests check.
5. **Filters by difficulty**, once per task: a smaller model tries twice first, and a task it solves both times is dropped. Then two frontier agents attempt the task, for at most 4 hours a trial, and it is kept only if at most 40% of trials pass.
6. **Audits and repairs**: an auditor reads the trajectories, and broken tasks are fixed and sent back through the cheap checks. The frontier trials are not run again.
7. **Reports** to a reviewer, who accepts or rejects.
8. **The final cut** is made by hand: the owner and the leads bring the accepted set down to the index's size with `hm cut TASK <why>`, which also takes the task out of the list and the task store.

Tasks run in parallel in working cells. A person decides three things: the brief, the selection, and each verdict. The design, the cost estimate, and the reasons behind each choice are in [DESIGN.md](DESIGN.md).

## Quick start

You need Python 3.12 or later and [uv](https://docs.astral.sh/uv/). A simulated run needs no keys and no Docker.

**1. Install it, and say who you are.**

```bash
cd harbor-master        # after cloning this repository
uv tool install --editable .
hm init
```

`uv tool install` puts the `hm` command on your path, so nothing needs `uv run`. `--editable` makes it follow the code in this folder. `hm init` is the whole of getting started, one box at a time, and it is the same for everyone:

| Box | What it asks |
|---|---|
| 1 to 5 | Your name, affiliation, email and GitHub handle, and then it makes your signing key |
| 6 | An index to join, by its name on the list or its repository's address. Left empty, a practice index is made in `hm-practice`, on this machine only |
| 7 | Your keys: where the keys file is, to fill in with an editor. The demo needs none |
| 8 | A simulated demo on a made-up benchmark of 10 tasks: *Curate* adds it and runs it through the stages, *Evaluate* fetches its to-do list and runs its tasks on the eval screen |

That last box is the only choice. A machine runs 4 tasks and 16 containers at once unless its `.hm/hm.toml` says otherwise.

**Run it again** to change your details. Each box shows what this machine has: press enter to keep it, or type over it. The last box saves them and keeps your signing key, so every record you signed stays yours. If you change your GitHub handle, send each index's owner the new line it prints.

`hm` alone does nothing: it lists the commands.

**2. Get an index, and contribute.** `hm indexes` lists the indexes there are, and `hm open INDEX` clones one into a folder here. Inside that folder there are two ways to contribute, one for each stage of an index:

```bash
hm curate          # stage 1: build the index. Opens the curate screen
```

```bash
hm eval            # stage 2: evaluate the released index. Opens the eval screen
```

Add `--demo` to either for a first look: a made-up benchmark of 10 tasks, on simulated models. It spends nothing, nothing of it enters the index, and it is a walk-through: as the run reaches each stage, the tip line says what that stage does.

**3. Answer the run.** Nothing is typed on the curate screen either: it has no commands. Whatever the run needs from you, it asks in a box that opens by itself and has to be answered:

| Box | What you do |
|---|---|
| A brief | Say in a sentence or two what matters in the benchmark |
| The selection | Say how many of the ranked tasks go on |
| A report | Read it, then type `accept` or `reject <why>` under it |
| The budget | When nine tenths of it is spent: raise it, or keep it |
| A trial that failed to run | It says what ran and why it failed: retry it, or discard it |

**No run needs its terminal.** `ctrl+l` leaves the screen and the run goes on, and so does closing the window or losing the connection. This holds for curating and for evaluating. `hm curate` or `hm eval`, inside the index's folder, shows the run again. There is one run per index, and it stays until you end it with `ctrl+q`.

**Light or dark.** `ctrl+t` switches every screen between dark and light while it is open. The choice is remembered on this machine.

**4. Look at what happened.** The index's owner and its leads see it on the dashboard, `hm open` inside the folder: Status has where every task stands, and Leaderboard how each agent did. Anyone sees what is left to evaluate with `hm todo`.

## An index

Harbor-Master holds no index. Each index is a git repository of its own, and a clone of it is your folder for it. Every `hm` command about an index is typed inside that folder. Its `about.toml` carries a canary line that marks the repository as an index: `hm open` refuses to clone one without it.

| In the repository | Says | Changed by |
|---|---|---|
| `about.toml` | What the index is: its canary, name, title, description, and where its accepted tasks are kept | The owner, by editing it |
| `protocol.toml` | The curation protocol: every setting that can change a result | The owner, by editing it and then `hm lock` |
| `benchmarks.toml` | The one list of benchmarks that may be curated, each with its name, description, paper, repository and pinned release | Anyone on the contributor list, with `hm benchmarks add URL`. `hm` writes the file from the signed records |
| `roster.toml` | The agent configurations benchmarked on the index | The owner, by editing it at any time, then `hm sync`. Never locked |
| `contributors.toml` | Who may contribute, and their signing keys | The owner, with `hm contributors add`. No lock |
| `skills/` | What the agents that curate this index are told. Copied from the tool's templates, and the owner's to reword | The owner, by editing them and then `hm lock`, which also copies in any that are missing |
| `adapters/` | For a benchmark that is not in Harbor format: the code that turns it into Harbor tasks | The run that first takes the benchmark |
| `ledger/` | Every decision, trial and verdict, signed | Everyone on the contributor list, through `hm` |
| `reports/` | The report on each task that reached one | `hm` |
| `index.toml` | The accepted tasks | `hm`, from the signed verdicts |

Your own machine's files are in `.hm/` inside the clone, which git ignores: your settings ([`hm.example.toml`](hm.example.toml)), the benchmarks you fetched, your runs, and what simulated runs recorded.

**The lock, and its version.** `hm lock`, which only the owner can run, signs what can change a curation's result: the settings and the skills. A run whose copy differs from the locked one is refused, and a real run needs a lock. Every index starts at version `0.0.0`, and a lock keeps the version unless the owner asks for a new one: `hm lock patch` for a small change such as a skill's wording (`0.0.1` to `0.0.2`), `hm lock minor` when settings changed (`0.1.0` to `0.2.0`). Version `1.0.0` is the index's release, which `hm release` sets.

**The roster is never locked.** It says which agents are evaluated, not how tasks are curated, so it changes no curation result. The owner adds a configuration at any time, before the release and after it, as new models come out: edit `roster.toml`, then `hm sync`. A new configuration adds its cells to the to-do list. Every trial record names its agent, version, model and effort, so no change to the roster alters a result already recorded.

**What else needs no lock.** Benchmarks, adapters and contributors change no result, so each is vouched for by a signed record of its own: a benchmark by whoever added it, an adapter by whoever checked it, the contributor list by the owner. A workspace also remembers the owner's key from the day it joined, and refuses a copy that names another owner.

### Join one

This is the usual way in, and it is the same for everyone:

| Type | What happens |
|---|---|
| `hm init` | Once per person. It makes your signing key, and prints one line to send an index's owner |
| `hm indexes` | The indexes that are listed |
| `hm open INDEX` | By its name on that list, or by the address of its repository. It is cloned into a folder here and checked to be an index |

The owner pastes your line in a shell, which adds you at once and needs no lock. `hm sync` brings it in. From then on you may add benchmarks, run any of them, and sign verdicts. Until then you can only run simulations.

### Start your own

An index is started and managed in a shell, and what it is made of is files its owner edits. Create an empty repository on GitHub first, and put a Hugging Face token that can write the task store in your keys file as `HF_TOKEN`.

```bash
hm new my-index --tasks my-lab/my-index-tasks --repo my-lab/my-index
cd my-index
```

| Then | How |
|---|---|
| Say what it is, and set it up | Edit `about.toml`, `protocol.toml`, `roster.toml` and the files under `skills/` |
| Lock it | `hm lock`. A real run needs a lock, and so does every later change to `protocol.toml` or `skills/` |
| Change the roster or the description | Edit `roster.toml` or `about.toml` at any time, then `hm sync` |
| Let someone in | `hm contributors add ...`, with the line they sent you. `hm contributors role HANDLE lead` names a lead |
| Make the final cut | `hm cut TASK why`, and `hm restore TASK` to undo one. The owner and the leads |
| See where it stands | `hm open`, the dashboard |
| Work in it, like everyone else | `hm curate`, and `hm eval` once it is released |

The agents to compare are listed in `roster.toml`, which explains each entry and names the ten presets. An entry can be one line, `use = "claude-opus-5.5"`, or written out in full with any agent the installed Harbor has.

**The list of indexes.** [`indexes.toml`](indexes.toml) in this repository lists the indexes that asked to be listed, each with its repository and its owner's key. It holds addresses only: everything else about an index is read from its own repository. To list yours, type `hm entry` in its folder and add what it prints in a pull request here. The check on the pull request clones the repository, so it must be readable by anyone.

## Two stages

An index is built, and then it is evaluated. These are two ways to contribute, with separate commands and separate records.

| | Stage 1: curate | Stage 2: evaluate |
|---|---|---|
| Open | While the index is being built | After its owner types `hm release`, which fixes the tasks as version 1.0.0. The roster can still grow |
| How | `hm curate`, inside the index's folder, which opens the curate screen | `hm eval`, inside the index's folder, which opens the eval screen |
| Who | Contributors | Anyone on the contributor list. An `evaluator` (`hm contributors add ... --role evaluator`) can do this and nothing else |
| Records | `ledger/` | `eval/`, one file per person |

**The to-do list** is every accepted task, with every configuration on the roster, `trials` times (3, in `[eval]` of the protocol). It is not stored: everyone computes the same list from the index, the roster and the trials there are. `hm todo` shows it.

**The eval screen** is laid out as the curate screen is: the roster's configurations where that one has its benchmarks, the working cells, what happened last, and a tip. It takes cells, runs them, and takes more until none is free. Like a curation run it goes on in the background when its terminal closes, and a trial that fails to run opens a box: retry it, or discard it. `hm eval --plain` does one take with no screen.

**Taking work.** `hm eval --take K` pulls, takes the next K free cells (a cell is one task with one configuration), and pushes a signed claim that holds them for `claim_hours` (4). The push is the lock: of two claims on a cell, the earlier holds it. The cells run side by side. When they are done, or the claim has run out, `hm eval` takes more. A configuration your machine has no key for is left to others, and nobody holds more than `max_claim` cells (20).

**Difficulty trials are reused.** A trial is identified by the task's version, the agent and its version, the model, the reasoning effort and the trial's number, not by the protocol's version. So the difficulty trials of stage 1 already fill their slots and are not run again, unless the task was repaired after them: the accepted version is then one no agent has attempted. The leaderboard says how many of a configuration's trials are ones the tasks were selected on.

**What a trial leaves.** One signed line in `eval/`, with the outcome, the tokens, the cost and the time, and the digest of its Harbor trial folder. The folder itself (trajectory, verifier output, `config.json`, `lock.json`, the result) goes to `trials/` in the task store as one archive, never to git. A trial that fails for an infrastructure reason fills no slot, and after `retries` (2) more failures the cell is given up and shown in `hm todo`.

## Benchmarks

A run takes any benchmark on the index's list, and nothing else: `hm benchmarks` shows the list. **Anyone on the contributor list adds one, with no step by the owner:**

1. `hm benchmarks add URL`, with the address of the benchmark's repository. It first brings in what others added, and refuses a repository or an ID that is on the list already, saying who added it. Then the code pins the newest tag and its commit, finds where the tasks are, and reads the licence. A small agent reads the repository and drafts the rest: name, description, paper, and the date the benchmark was released, which the recency gate compares. Where the repository does not say, the date of the pinned commit stands in until you correct it.
2. `hm benchmarks confirm` adds it, and `hm benchmarks revise WORDS` says what to change, in plain words or as `paper: https://...`. A benchmark needs a name, a description, a repository, a release date, and a paper (`paper: none` if it has none).
3. Your signed record puts it on the list for everyone, and writes it into `benchmarks.toml`, the one list. Only you or the owner can take it off, with `hm benchmarks remove ID`.

Adding spends nothing and admits nothing: the brief, the selection and the verdicts still decide what enters the index. A run does the rest.

- **Fetching.** The run fetches a benchmark that is not on the machine yet, at its pinned commit.
- **Adapting.** If the tasks are not Harbor task folders, the run builds an adapter, in Harbor's own adapter layout. The worker model writes it, and it is checked without a model: it runs, its tasks are complete, a second run writes the same ones, and a few reference solutions pass through Harbor. A failing one goes back to the worker model, then to the frontier model.
- **Asking a person only on failure.** If the models cannot make it pass, a box asks for one of two things: fix the files and check again, or abandon the benchmark. An abandoned benchmark is refused to every run, with the reason. To try it again, take it off the list and add it again.
- **The same tasks for everyone.** An adapter that passes is committed to the index with a signed record of its checks, and runs in a container with no network. Every later run uses that same version.

The demo's made-up benchmark needs no entry and runs under `--simulate` only.

## Working with others

- **Who may contribute.** `contributors.toml` lists the people allowed to add benchmarks, submit records and sign verdicts, each identified by a GitHub handle and a public key. Everyone on it may work on any benchmark. Only the owner changes it, with `hm contributors add` and `hm contributors remove`, and signs each change: a copy that differs from the one the owner signed is refused.
- **Nobody runs a task twice.** Everyone works in a clone of the same repository. A run pulls before it starts, claims its tasks, and pushes the claim: if someone else claimed first, the push is refused and the run gives those tasks up. A task someone holds, or one that already has an outcome, is not run again.
- **What a run pushes.** Its own records and reports, never a change to the files that define the index. `hm sync` pulls and pushes by hand, checks the ledger first, and puts in the task store any accepted task that did not reach it when it was accepted.
- **Reviewing.** A report reaches every clone, so a reviewer on another machine can read it and sign a verdict.

## The accepted tasks and the leaderboard

A task enters the index when a contributor accepts it. `index.toml` lists the accepted tasks, written from the signed verdicts.

- **The task store.** Every index has one, named when the index is created. Each accepted task is kept there as one archive named by its digest, because tasks are too large for git and a later benchmark run must get exactly the version that was accepted. It is a Hugging Face dataset repository (`org/name` in `about.toml`), created private by `hm new`.
  - **Each person uses their own token.** Nobody's token is shared. Put `HF_TOKEN` in your keys file, and ask the owner for write access to the repository on Hugging Face. Reading a public store needs no token.
  - **A real run checks it first.** Before anything is spent, the run refuses if the store is not named, does not exist, or your token is missing. An accepted task is uploaded when its verdict is signed.
  - **If an upload fails,** the machine notes it, and its next `hm sync` tries again. To name another store, the owner changes `tasks` in `about.toml` and runs `hm lock`, which creates it.
- **Benchmarking an agent.** `hm eval --config NAME` runs a roster configuration on the accepted tasks, each at its accepted version, from the machine or from the store. It refuses a task whose accepted version it cannot get. `hm todo` shows what is still to run.
- **The leaderboard** ranks the configurations on the accepted tasks only, computed from the ledger. It is on the dashboard.

Trial archives and the probe's mutants are not shared: they stay on the machine that made them.

## Simulated runs

`--simulate` uses simulated models and trials, and spends nothing. A simulated run never counts as real:

- its records are kept in `.hm/simulated/` on your machine, never in the index's ledger, and never pushed;
- every record is marked as simulated inside its signature;
- a real ledger leaves out any that are copied in, and `hm sync` reports them.

So `--simulate` is safe in any index. The dashboard and `hm todo` show what simulated runs did while an index has nothing else, and say so.

## Keys

Real runs call models and need keys. `hm init` copies [`secrets.env.example`](secrets.env.example) to `secrets.env` in the Harbor-Master folder, readable only by you and ignored by git. Fill in the keys you have: Claude and Codex each take an API key or a subscription sign-in. The worker and frontier tiers run on Claude models, or on OpenAI models through Codex (`provider = "codex"` in `protocol.toml`), which uses the sign-in that `codex login` writes. A key is never typed into a screen, written to a settings file, or put in the ledger.

## Commands

**In any folder: four commands.**

| Command | Does |
|---|---|
| `hm init` | Asks who you are, in boxes, and makes your signing key. Then an index, your keys, and a simulated demo. Everyone starts here, and runs it again to change their details |
| `hm indexes` | Lists the indexes that are listed for anyone to join |
| `hm open INDEX` | Clones an index into a folder here, and checks its canary |
| `hm new INDEX --tasks ORG/NAME --repo OWNER/NAME` | Starts an index of your own. A plain command: what it needs is on its line |

`hm` alone is an error that lists these.

**Inside an index's folder: eleven more.** Anywhere else these are refused, with a line that says so. There are no other commands, hidden or not.

| Who | Command |
|---|---|
| Everyone | `hm benchmarks` and `hm curate` while the index is built, `hm eval` and `hm todo` once it is released, `hm sync` |
| The owner | `hm contributors add`, `role` and `remove`, `hm lock [patch \| minor]`, `hm release`, `hm entry` |
| The owner and the leads | `hm open` for the dashboard, `hm cut TASK why`, `hm restore TASK` |

**Three screens, and no commands on any of them.**

- **The curate screen** (`hm curate`) and **the eval screen** (`hm eval`) are for contributing, and show only their own run. Each asks what it needs in a box that has to be answered, and has four keys: `ctrl+p` pauses, `ctrl+l` leaves and the run goes on, `ctrl+t` switches light and dark, `ctrl+q` ends the run, after asking. On the curate screen a cell's number opens its task.
- **The dashboard** (`hm open`, inside the index's folder) is for the index's owner and its leads, and is read only. It shows where the index stands: how far each benchmark is curated, how far the released index is evaluated, the protocol, the roster and the contributors. Buttons open the details in a box: Leaderboard, Tasks, Status, To do, Credits, Benchmarks, Protocol, Skills. Nothing is started or changed from it.

<p align="center">
  <img src="assets/onboarding/index-01-dashboard.png" width="100%" alt="The index's dashboard: a header, six figures (benchmarks, tasks accepted, reports to review, evaluated, contributors, spent), panels for curation and evaluation with a bar per row, the roster, the protocol and the contributors, and a row of buttons that open the details.">
</p>

A script or a CI job reaches every command with `python -m harbor_master NAME -w FOLDER`.

## Repository

```
src/harbor_master/             one package per component: cli, tui, interact, engine, backends, curation, ledger, tasks, core
src/harbor_master/skills/      the templates of what each agent is told, one file per job. Each index keeps its own copies
src/harbor_master/templates/   the blank files a new index starts from
tests/                         one file per component
tools/                         screenshot.py, which captures the pictures in assets/ from the running screen
indexes.toml                   the list of indexes: name, repository and owner of each
hm.example.toml                one machine's own settings: parallelism and budget
secrets.env.example            the keys file to copy and fill in
DESIGN.md                      the design, the cost estimate, and the implementation status
```

## Citation

```bibtex
@misc{openssi2026harbormaster,
  title={Harbor-Master: Scalable Multi-Agent Infrastructure for Open, Continual Benchmark Indexes},
  author={The OpenSSI Team},
  year={2026}
}
```
