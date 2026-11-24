---
type: Design
title: "Agent-written code walkthroughs"
description: "Data model, CLI contract, and flows for ordered walkthroughs of diff and source excerpts, each step a reply thread."
tags: ["agents", "walkthroughs", "cli", "storage"]
generated: { by: "claude-code/claude-opus-5-5", at: "2026-11-24T00:00:00Z" }
sources:
  - resource: "Repository worktree: mods/Codewalker/backend.py, tests/test_backend.py"
    title: "Walkthrough store schema v6, commands, and backend tests"
  - resource: "Repository worktree: mods/Codewalker/walkthrough.js, walkthrough-model.js, main.js, client.js, model.js, scripts/check-ui.py, tests/test_ui.mjs"
    title: "Native walkthrough pane, Review list, Send for step threads"
  - resource: "Design discussion with the user, 2026-11-23 to 2026-11-24"
    title: "Decisions: frozen excerpts, step body as first message, linear steps, agent-only authoring, progress later"
status: draft
---
# Agent-written code walkthroughs

Status: implemented. Backend (store, CLI, agent skill) and the native pane: outline, step view, replies, Send, archive and delete. Per-person progress is not implemented.

## Core idea

A **walkthrough** is an ordered list of **steps** that an agent writes for the reviewer. Each step has a title, a Markdown body and an optional view: a range of the diff, or lines of a file. Each step owns one ordinary thread. The first message of that thread is the step body. Human replies, edits, resolve, `awaitingReply`, delivery and Send reuse the thread machinery unchanged.

## Decisions

| Question | Decision |
| --- | --- |
| Excerpt lifetime | **Frozen.** The backend copies the rows at write time. A step shows `validity: current \| stale`; it never re-reads or retargets. |
| Step body | The first message of the step's thread. `edit` changes it and keeps the old body; `delete` of that message is refused. |
| Shape | Linear only. `walkthrough-append` can insert after a step. |
| Authoring | Only `actor.kind:"agent"` creates or appends; only the author changes title and summary. Humans reply, resolve, archive and delete (`confirmed:true`). |
| Progress | Later. The pane keeps only an in-memory position for now. |
| Review discussions | Step threads are not in `list`/`review` `threads`, so Review and existing agent scripts are unaffected. Summaries are in `walkthroughs`. |
| History clear | `history-clear` keeps walkthroughs and their threads; `walkthrough-delete` removes them. |

## Data model (store schema v6)

```sql
walkthroughs (id, review_id → reviews, title, summary, author JSON,
              comparison JSON,      -- {scope, baseRef, commitOid?}
              status,               -- open | archived
              created_at, updated_at)
walkthrough_steps (id, walkthrough_id → walkthroughs, position,
                   thread_id → threads UNIQUE, title, view JSON | NULL)
```

- Keyed by `review_id` (worktree + branch) like threads. Same SQLite file, one shared `revision`, the same idempotency receipts.
- Positions are contiguous from 0. Insertion shifts later steps.
- A step thread's `anchor` holds the comparison evidence at write time: `{walkthroughId, stepId, path, scope, baseRef, baseOid, headOid, snapshotId, commitOid?}`. Replies recheck against it, as for commit anchors.
- Every thread object now has `walkthrough`: `null`, or `{id, title, stepId, position, stepTitle, stepCount}`.

### Step views

| `kind` | Input | Frozen output |
| --- | --- | --- |
| `diff` | `path`, optional `side` (`old`/`new`) + `startLine` + `endLine` | `{path, oldPath, status, side, startLine, endLine, fileFingerprint, hunks:[{rows}]}`. A range takes the rows in it per hunk, plus adjacent rows of the other side (a new-side range keeps the lines it replaced). No range: the whole file diff. |
| `source` | `path`, `rev` (`base`/`head`), `startLine`, `endLine` | `{path, rev, startLine, endLine, fingerprint, hunks:[{rows}]}`; rows are `context` with the number on `oldLine` (base) or `newLine` (head). Any regular UTF-8 file, also unchanged ones. `head` is the worktree in uncommitted and branch scope. |
| `null` | – | Markdown only. |

Rows use the review row shape `{kind, oldLine, newLine, text}`. Callers never supply rows. Limits: 100 steps, 400 lines or rows per step, 512-byte titles, 64 KiB bodies, 1 MiB mutation JSON (`data-parts` splits it).

### Validity

- `null` view: always `current`.
- Otherwise `stale` unless the walkthrough's comparison still resolves to the same `scope`, `baseOid` and `commitOid` as the step.
- `diff`: the file is still in the diff with the same `oldPath` and both-side fingerprint.
- `source`: `base`, and any side of a commit or PR comparison, are immutable. A worktree `head` compares the file digest.
- A walkthrough is `stale` when any step is.

## CLI contract

`hello` reports `helperVersion: 3` and capability `walkthroughs`. A UI must not use these commands on a remote helper without it.

Reads (never write the store):

| Command | Result |
| --- | --- |
| `walkthroughs` | `{reviewId, revision, walkthroughs:[summary]}` |
| `walkthrough --walkthrough <id>` | `{reviewId, revision, walkthrough}`: summary fields plus `steps:[{id, position, title, view, validity, thread}]` and `validity` |
| `list`, `review` | gain `walkthroughs:[summary]` |
| `export` | gains one line per walkthrough |

Summary: `{id, title, summary, status, author, comparison, createdAt, updatedAt, stepCount, openThreads, awaitingReply, replies}`.

Mutations use the common envelope (`commandId`, `reviewId`, `expectedRevision`, `actor`):

| Command | `--data` fields | Result |
| --- | --- | --- |
| `walkthrough-create` | `scope`, `base`/`commit` as for `create`, `snapshotId`, `title`, optional `summary`, `steps:[{title, body, view?}]` | `{walkthrough}` |
| `walkthrough-append` | `walkthroughId`, `snapshotId` (fresh read of the pinned comparison), `steps`, optional `afterStepId` | `{walkthrough}` |
| `walkthrough-update` | `walkthroughId`, and `title`, `summary` (author) or `status` (anyone) | `{walkthrough}` |
| `walkthrough-delete` | `walkthroughId`, `confirmed:true` for a human who is not the author | `{deleted:{walkthroughId, steps}}` |
| `reply`/`edit`/`resolve`/`deliver` | unchanged, with the step's thread or message ID | as before |

All steps are validated in one transaction. A bad step fails the whole command with `details.step` (0-based); nothing is written. A changed diff fails `snapshot_conflict`.

`open --walkthrough <id>` checks that the walkthrough exists in this checkout's review, then splits a pane with `props:{framed:true, folder, walkthrough:<id>}` and returns `{pane, folder, walkthroughId, scope}`. It takes no `--base`/`--commit`. Local Herdr terminals only.

`skill --topic walkthrough` prints the agent recipe (read the diff, write steps, create, open, answer). The default `skill` text tells agents that a thread with `walkthrough` is a step.

## Flows

**Agent writes a walkthrough.** `review` (and `review --file` for line numbers) → compose steps → `walkthrough-create` (retry only on `revision_conflict`) → `open --walkthrough` → read `walkthrough --walkthrough` for replies and answer with `reply`.

**Human reads and replies.** The pane opens at step 1. It shows the outline (title, summary, numbered steps with reply counts and a red dot for changed code) and the step: its Markdown body, the frozen excerpt (Review's diff rows, read-only, with gap rows between hunks), and the discussion. Previous/Next and the ←/→ keys move between steps when no reply or edit is open. Wide panes put the outline beside the step; narrow ones stack them with a native divider.

Replies use Review's composer, write queue and durable drafts. Moving to another step saves an open reply as a draft; **Continue draft** restores it. A draft records `walkthroughId`, so Review's draft list hides it. Edit and Delete work on your own replies only; the agent's step body cannot be changed in the UI. Resolve works per step.

Send uses the step threads as the view's threads: each section is headed `Walkthrough "<title>", step n of N: <step> · thread <id>`, pastes only the replies (not the agent's own step text), and names the `walkthrough --walkthrough <id>` command once per walkthrough, at the end.

**After the pane closes.** Nothing is lost. When the review has walkthroughs, Review's toolbar shows a book button with the open count; its list opens one in place, and **Review** (arrow) returns to the comparison that was shown. Archive moves a walkthrough below the open ones; Delete asks first and also removes its reply drafts.

Implementation: `walkthrough.js` renders with the host's parts; `walkthrough-model.js` holds pure helpers; the host (`main.js`) owns state and writes. In walkthrough mode `refresh`/`read` go to `readWalkthrough`, the store watch refreshes on agent writes, and a remote helper without the `walkthroughs` capability is refused before it runs.

## Pending

- Durable per-person progress (last step, read marks).
