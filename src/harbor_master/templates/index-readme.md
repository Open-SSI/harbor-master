# $title

An index of hard, verified tasks, curated with [Harbor-Master](https://github.com/Open-SSI/harbor-master). This repository is the index: the files that define it, and the signed ledger of every decision, trial and verdict made for it.

## Take part

Install Harbor-Master and say who you are:

```bash
uv tool install git+https://github.com/Open-SSI/harbor-master
hm init
```

Send the owner the line `hm init` prints, so they can add you. Then clone this index, put your keys in its `secrets.env`, and curate:

```bash
hm clone $clone && cd $name
hm curate
```

Your clone has its own `secrets.env` (your keys) and `hm.toml` (how your machine runs it), made from `secrets.env.example` and `hm.example.toml`. Git ignores both.

## What is here

| File | Holds | Changed by |
|---|---|---|
| `about.toml` | Its name, description, version and task store | The owner |
| `protocol.toml`, `skills/` | Every setting that can change a result, and what each agent is told | The owner; every record carries their hash |
| `benchmarks.toml` | The benchmarks that may be curated, each pinned to a release | `hm benchmarks add URL`, by anyone on the list |
| `contributors.toml` | Who may contribute, with their signing keys | The owner, with `hm contributors` |
| `hm.example.toml`, `secrets.env.example` | What each clone's own `hm.toml` and `secrets.env` are made from | The owner |
| `adapters/` | Code that turns a benchmark into Harbor tasks | The run that first takes the benchmark |
| `ledger/`, `reports/` | Every signed record, and the report on each kept task | `hm` only; checked before every push |
| `index.toml` | The accepted tasks | `hm` only, from the signed verdicts |

`hm` keeps this repository in step by itself: every command pulls first and pushes what is new. The owner raises `version` in `about.toml` when a change to `protocol.toml` or `skills/` would change results.
