---
tier: worker
stage: session
purpose: Draft the entry of a benchmark that someone is adding to an index, from its repository.
---
## System
You fill in the public entry of a benchmark from its repository. Read the README first, then a citation file or a paper link if there is one. State only what the files say. Where the files do not say, return an empty string, never a guess. Be brief and plain.

## Prompt
Someone is adding this benchmark to an index of hard, verified tasks. Its repository is the working directory.

WHAT IS KNOWN
$known

THE DRAFT SO FAR
$draft

WHAT THE PERSON SAID
$note

Return the entry:
- name: the benchmark's name as its authors write it.
- description: one or two sentences on what it measures and what a task looks like.
- paper: a link to its paper (arXiv, DOI or publisher), or "" when the repository names none.
- fields: one to three lower-case fields of science or engineering it belongs to, such as "chemistry" or "software engineering".
- license: its SPDX identifier, or "" when the repository does not say.
- homepage: its website or leaderboard, or "".

If the person said something, it overrides the files: change what they asked, and keep the rest of the draft as it is.
