# $title

An index of hard, verified tasks, curated with [Harbor-Master](https://github.com/Open-SSI/harbor-master). This repository is the index: the files that define it, and the signed ledger of every decision, trial and verdict made for it.

## Take part

1. Install Harbor-Master and say who you are:

   ```bash
   uv tool install git+https://github.com/Open-SSI/harbor-master
   hm init
   ```

2. Send the owner the line `hm init` prints. They add you to the contributor list, and give your accounts write access to
   this repository on GitHub and to $store.

3. Clone this index:

   ```bash
   hm clone $clone && cd $folder
   ```

4. Put your keys in the clone's `secrets.env`. Git ignores it, and the file says what each key is for:
   - Claude: an API key, or a subscription token from `claude setup-token`
   - OpenAI: an API key, or a ChatGPT subscription signed in once with `codex login`
   - OpenRouter: an API key, for the decision model
   - Hugging Face: your own `HF_TOKEN`, with write access to $store

5. Curate: `hm curate`. It checks every key before anything is spent, and says what is missing.

How your machine runs it (work cells, containers) is in the clone's `hm.toml`, which git ignores too.

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
