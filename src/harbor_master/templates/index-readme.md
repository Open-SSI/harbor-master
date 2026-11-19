# $title

An index of hard, verified tasks, curated with [Harbor-Master](https://github.com/Open-SSI/harbor-master).
This repository is the index: the files that define it, and the ledger of every decision,
trial and verdict made for it.

| File | Says | Who may change it | How a change is caught |
|---|---|---|---|
| `about.toml` | What the index is: its name and description | The owner | Not locked: it changes no result |
| `protocol.toml` | The curation protocol: every setting that can change a result | The owner | Its checksum is locked in the ledger. A run with a different copy is refused |
| `benchmarks.toml` | Which benchmarks may be curated, each pinned to a release | The owner | Its checksum is locked in the ledger with the protocol |
| `roster.toml` | Which agent configurations are benchmarked on the index | The owner | Its checksum is locked in the ledger with the protocol |
| `contributors.toml` | Who may curate, in which fields, and their signing keys | The owner | Its checksum is locked in the ledger with the protocol, and every record is checked against it |
| `skills/` | What the agents that curate this index are told: the adapter, the gates, the ranking, the probe, the audit | The owner | Part of the protocol's checksum |
| `adapters/` | For a benchmark that is not in Harbor format: the code that turns it into Harbor tasks | The owner | Its checksum is locked in the ledger with the protocol |
| `ledger/` | Every record, in one file per person and run | Everyone on the list, through `hm` | Signed and chained: `hm ledger verify` |
| `reports/` | The report on each task that reached one, which a reviewer reads before signing a verdict | Nobody: `hm` writes them | Each record of a report holds its checksum |
| `index.toml` | The accepted tasks | Nobody: `hm` writes it from the signed verdicts | `hm ledger verify` rejects anything the verdicts do not support |

To take part: `hm init`, send your public key to the owner, then `hm join` with this
repository's address. After changing a locked file, the owner runs `hm protocol lock`.
