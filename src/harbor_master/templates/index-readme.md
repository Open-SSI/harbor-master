# $title

An index of hard, verified tasks, curated with [Harbor-Master](https://github.com/Open-SSI/harbor-master).
This repository is the index: the files that define it, and the ledger of every decision,
trial and verdict made for it.

| File | Says | Who may change it | How a change is caught |
|---|---|---|---|
| `about.toml` | What the index is: its name and description | The owner | Not locked: it changes no result |
| `protocol.toml` | The curation protocol: every setting that can change a result | The owner | Its checksum is locked in the ledger. A run with a different copy is refused |
| `benchmarks.toml` | The one list of benchmarks that may be curated, each with its name, description, paper, repository and pinned release | Nobody by hand: anyone who can curate adds one with `hm benchmarks add URL` | The list is what the signed records say. Editing the file changes nothing |
| `roster.toml` | Which agent configurations are benchmarked on the index | The owner, at any time, as new models come out | Not locked: every trial record names its agent, version and model, so a change never alters a result |
| `contributors.toml` | Who may contribute, and their signing keys | The owner, in a shell with `hm contributors add HANDLE KEY` | The owner signs each change, with no lock. A copy that differs is refused, and every record is checked against the list |
| `skills/` | What the agents that curate this index are told: the adapter, the gates, the ranking, the probe, the audit | The owner | Part of the protocol's checksum |
| `adapters/` | For a benchmark that is not in Harbor format: the code that turns it into Harbor tasks | The run that first takes the benchmark | A run uses only a version that a signed record says passed its checks |
| `ledger/` | Every record, in one file per person and run | Everyone on the list, through `hm` | Signed and chained: `hm sync` checks them |
| `reports/` | The report on each task that reached one, which a reviewer reads before signing a verdict | Nobody: `hm` writes them | Each record of a report holds its checksum |
| `index.toml` | The accepted tasks | Nobody: `hm` writes it from the signed verdicts | `hm sync` rejects anything the verdicts do not support |

To take part: `hm init`, send the line it prints to the owner, then `hm open` with this
repository's address. After changing `protocol.toml` or `skills/`, the owner runs `hm lock`.
