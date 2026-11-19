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
2. **Admits** tasks that an agent must execute, that ship a reference solution, and that a program can verify quickly. Multiple choice and question answering are filtered out.
3. **Ranks** the admitted tasks by importance under a brief from the field lead: each is scored once, and the ones near the top are then compared in pairs. It **selects** the top ones: at most 24 per benchmark, and at most a quarter of it. The lead approves the selection before any trial is spent.
4. **Verifies and probes** each selected task: the reference solution must pass, a no-op must fail, and the verifier must reject wrong answers and accept right ones in another form.
5. **Filters by difficulty**: two frontier agents attempt the task, for at most 4 hours a trial, and it is kept only if at most 40% of trials pass.
6. **Audits and repairs**: an auditor reads the trajectories, and broken tasks are fixed and sent back through the checks.
7. **Reports** to a reviewer, who accepts, rejects, or sends back. A benchmark may have at most 8 tasks in the index, so a ninth must replace one.

Tasks run in parallel in working cells. A person decides three things: the brief, the selection, and each verdict. The design, the cost estimate, and the reasons behind each choice are in [DESIGN.md](DESIGN.md).

## Quick start

You need Python 3.11 or later and [uv](https://docs.astral.sh/uv/). A simulated run needs no keys and no Docker.

**1. Install it, and open it.**

```bash
cd harbor-master        # after cloning this repository
uv tool install --editable .
hm
```

`uv tool install` puts the `hm` command on your path, so nothing needs `uv run`. `--editable` makes it follow the code in this folder.

`hm` on its own opens the home screen. It needs nothing set up: a checklist shows the steps and where you are, and the tip under it says what to do now, with the commands for it.

<p align="center">
  <img src="assets/tui-home.png" width="100%" alt="The Harbor-Master home screen: a checklist of eight steps with the first one done, and a tip panel offering to see the listed indexes, join one, start one, or try a demo.">
</p>

**2. Follow the tips.** For a first look, three commands are enough:

| Type | What happens |
|---|---|
| `/init` | It asks your name, affiliation, email and GitHub handle, and makes your signing key |
| `/demo` | It starts an index called `hm-demo` in this folder, with a made-up benchmark in it |
| `/run --simulate` | The run screen opens, on simulated models and trials. It spends nothing |

**3. Answer the run.** On the run screen, commands start with `/`, and `tab` completes them and their arguments. The line marked `tip` says what the run needs from you. It stops twice to ask, in the amber panel: first for the lead's brief (`/brief demo-bench <what matters>`), then to approve the selection (`/approve demo-bench`). When reports are ready it tells you: `/review` lists them, and `/accept` or `/reject` signs your verdict.

**The run does not need its terminal.** `ctrl+l` leaves the screen and the run goes on. So does closing the window. `hm run` opens it again, and `hm run NAME` adds another benchmark to it. There is one run per index, and it stays, also when it has finished, until you end it with `ctrl+q`.

**4. Look at what happened.** `/status --simulated` shows where every task stands, and `/stats --simulated` writes the statistics.

Every command on the home screen is also an `hm` command in a shell: `/run --simulate` is `hm run --simulate`. `hm guide` prints the checklist and the tip as text, and `hm --help` lists every command.

## An index

Harbor-Master holds no index. Each index is a git repository of its own, and a clone of it is your workspace: `hm` works from anywhere inside it.

| In the repository | Says | Changed by |
|---|---|---|
| `about.toml` | What the index is: name, title, description, and where its accepted tasks are kept | The owner |
| `protocol.toml` | The curation protocol: every setting that can change a result | The owner, by locking |
| `benchmarks.toml` | The benchmarks that may be curated, each pinned to a release | The owner, by locking |
| `roster.toml` | The agent configurations benchmarked on the index | The owner, by locking |
| `contributors.toml` | Who may curate, in which fields, and their signing keys | The owner, by locking |
| `skills/` | What the agents that curate this index are told. Copied from the tool's templates, and the owner's to reword | The owner, by locking |
| `adapters/` | For a benchmark that is not in Harbor format: the code that turns it into Harbor tasks | The owner's run, or the owner |
| `ledger/` | Every decision, trial and verdict, signed | Everyone on the contributor list, through `hm` |
| `reports/` | The report on each task that reached one | `hm` |
| `index.toml` | The accepted tasks | `hm`, from the signed verdicts |

Your own machine's files are in `.hm/` inside the clone, which git ignores: your settings ([`hm.example.toml`](hm.example.toml)), the benchmarks you fetched, your runs, and what simulated runs recorded.

**The lock.** `hm protocol lock`, which only the owner can run, signs the settings, the skills, the three lists and every adapter into the ledger. A run whose copy differs is refused, and a real run needs a lock. A workspace also remembers the owner's key from the day it joined, and refuses a copy that names another owner.

### Start your own

```bash
hm init                                   # once per person: who you are, and your signing key
hm index new my-index --title "My Index"  # a new repository, with you as its owner
cd my-index
```

Then:

1. Describe it in `about.toml`, list benchmarks in `benchmarks.toml`, and list the agents to benchmark in `roster.toml`. Each file explains its fields.
2. `hm protocol lock` signs it all.
3. `hm keys init` creates the keys file for real runs, and `hm doctor` shows which keys are found.
4. `hm index connect URL` shares it through an empty git repository, so that others can join.
5. `hm run` curates. `hm run --simulate` tries it on simulated models, with no lock and no keys.

`hm guide` says which of these is next at any time.

### Join one

```bash
hm init              # once per person. It prints your public key
hm indexes           # the indexes that are listed
hm join NAME         # or: hm join URL
```

Send your public key to the index's owner, who adds you to `contributors.toml` with the fields you may curate, and locks the change. `hm sync` brings it in. Until then you can only run simulations.

**The list of indexes.** [`indexes.toml`](indexes.toml) in this repository lists the indexes that asked to be listed, each with its repository and its owner's key. It holds addresses only: everything else about an index is read from its own repository. To list yours, run `hm index entry` in it and add what it prints in a pull request here. The check on the pull request clones the repository, so it must be readable by anyone.

## Benchmarks

A run takes any benchmark on the index's list, and nothing else: `hm benchmarks` shows the list. The owner adds a benchmark with one entry in `benchmarks.toml`, pinned to a tag and a commit, and locks the change. `hm run` does the rest.

- **Fetching.** The run fetches a benchmark that is not on the machine yet, at its pinned commit.
- **Adapting.** If the tasks are not Harbor task folders, the owner's run builds an adapter, in Harbor's own adapter layout. The worker model writes it, and it is checked without a model: it runs, its tasks are complete, a second run writes the same ones, and a few reference solutions pass through Harbor. A failing one goes back to the worker model, then to the frontier model.
- **Asking a person only on failure.** If the models cannot make it pass, the run asks for one of two things: fix it and `/recheck`, or `/abandon` the benchmark. An abandoned benchmark is refused to every run, with the reason, until the owner builds an adapter that passes with `hm adapt ID`.
- **The same tasks for everyone.** An adapter that passes is locked into the index and runs in a container with no network. `hm adapt ID --from PATH` takes an adapter that exists already, such as one written for Harbor.

A local folder registered with `hm submit PATH --dev`, or made by `hm demo`, needs no entry and runs under `--simulate` only.

## Working with others

- **Who may contribute.** `contributors.toml` lists the people allowed to submit records and sign verdicts, each identified by a GitHub handle and a public key. `hm contributors` shows who can contribute, and each person curates only the fields listed for them.
- **Nobody runs a task twice.** Everyone works in a clone of the same repository. A run pulls before it starts, claims its tasks, and pushes the claim: if someone else claimed first, the push is refused and the run gives those tasks up. A task someone holds, or one that already has an outcome, is not run again. `hm claims` shows who holds what.
- **What a run pushes.** Its own records and reports, never a change to the files that define the index. `hm sync` pulls and pushes by hand.
- **Reviewing.** A report reaches every clone, so a reviewer on another machine can read it and sign a verdict.

## The accepted tasks and the leaderboard

A task enters the index when a contributor accepts it. `index.toml` lists the accepted tasks, written from the signed verdicts.

- **The task store.** Each accepted task is kept as one archive named by its digest, because tasks are too large for git and a later benchmark run must get exactly the version that was accepted. `tasks` in `about.toml` names the store: a Hugging Face dataset repository (`org/name`) or a folder. `hm tasks` shows where each copy is, `hm tasks push` and `hm tasks pull` move them.
- **Benchmarking an agent.** `hm eval --config NAME` runs a roster configuration on the accepted tasks, each at its accepted version, from the machine or from the store. It refuses a task whose accepted version it cannot get. `hm todo` shows what is still to run.
- **The leaderboard.** `hm leaderboard` ranks the configurations on the accepted tasks only, computed from the ledger.

Trial archives and the probe's mutants are not shared: they stay on the machine that made them.

## Simulated runs

`--simulate` uses simulated models and trials, and spends nothing. A simulated run never counts as real:

- its records are kept in `.hm/simulated/` on your machine, never in the index's ledger, and never pushed;
- every record is marked as simulated inside its signature;
- a real ledger leaves out any that are copied in, and `hm ledger verify` reports them.

So `--simulate` is safe in any index. To see what simulated runs did, add `--simulated` to a read command: `hm status --simulated`, `hm leaderboard --simulated`.

## Statistics

`hm stats` writes the statistics of the curation into `.hm/stats/`: a report, `stats.md`, and a folder `tables/` with one CSV per table. The report has four sections:

1. **What happened to the tasks**: the funnel from submitted to accepted, per benchmark, and why tasks stopped.
2. **What it cost**: calls, tokens and cost per model tier and per stage, and the runs through Harbor.
3. **What people had to do**: every decision a person made, and the benchmarks a run left out.
4. **Where the figures come from**: the commit and a checksum of the ledger it read, so the same report can be made again.

The figures cover everyone's records: in a shared index it pulls first. The folder is git-ignored and stays on your machine. `hm stats --simulated` does the same for simulated runs.

## Keys

Real runs call models and need keys. `hm keys init` copies [`secrets.env.example`](secrets.env.example) to `secrets.env` in the Harbor-Master folder, readable only by you and ignored by git. Fill in the keys you have: Claude and Codex each take an API key or a subscription sign-in. `hm doctor` shows which are found and what they let this machine run. A key is never typed into a screen, written to a settings file, or put in the ledger.

## Commands

| For | Commands |
|---|---|
| Getting started | `hm` (the home screen), `hm guide`, `hm init`, `hm key`, `hm demo` |
| An index | `hm index`, `hm index new`, `hm join`, `hm indexes`, `hm index connect`, `hm index entry`, `hm contributors` |
| What it runs under | `hm protocol`, `hm protocol lock`, `hm skills`, `hm skills diff`, `hm skills adopt`, `hm roster` |
| Benchmarks | `hm benchmarks`, `hm adapt`, `hm submit` |
| Curating | `hm run` (starts the run, or opens the one that is going), `hm run NAME` (adds a benchmark to it), `hm brief`, `hm review`, `hm claims`, `hm requests`, `hm sync` |
| Results | `hm status`, `hm stats`, `hm credits`, `hm ledger verify`, `hm tasks` |
| Benchmarking agents | `hm eval`, `hm todo`, `hm leaderboard` |
| This machine | `hm keys init`, `hm keys set`, `hm doctor` |

`hm COMMAND --help` describes each one.

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
