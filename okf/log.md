# Bundle Update Log

## 2026-11-25

- **Change**: Diff rows and walkthrough excerpts have syntax colors from herdr-reviewer's line tokenizer (`highlight.js`, MIT, `LICENSE-reviewer`) in theme ANSI colors without red. Rows are colored once, only as far as shown. The test loader in `tests/test_ui.mjs` now links each module once, from the top, because `model.js` has an import. `scripts/check-ui.py` reads a colored row's text joined. With a load average near 20, `herdr-app check` sometimes interrupted a render on its time limit; scene times matched the previous commit.

## 2026-11-24

- **Decision**: Walkthroughs freeze their excerpts, use each step thread's first message as the step body, are linear, and are written only by agents. Per-person progress is deferred. See [walkthroughs](/walkthroughs.md).
- **Change**: Backend store schema 6, helper version 3, capability `walkthroughs`, walkthrough read/mutation commands, `open --walkthrough`, and `skill --topic walkthrough`. The native pane and Send for step threads are not implemented.
- **Change**: Native walkthrough pane, Review's walkthrough list, Send for step threads, and the `walkthrough` preview scene. `scripts/check-ui.py` now ignores only the `herdr-app check` reload notice about the stranded refresh timer; it failed on that notice before this change as well.
- **Decision**: **Ask for a short reply** is renamed **Reply in thread**. Off, the agent can answer in chat; on, it must also answer in the thread. Send, `export` and the skill say `reply in thread required`. Sending is unchanged, and the `replyRequested` and `awaitingReply` fields keep their names.
- **Change**: Send gives the walkthrough read command once per walkthrough, at the end, not once per step. A step with no view has no path; Review's file list is not built in a walkthrough.

## 2026-11-17

- **Initialization**: Converted all six Markdown documents from `docs/` into OKF v0.2 concepts, preserving their content and recording their source Git revision.
- **Decision**: Replaced `docs/` with `okf/` at the user's request. Moved the five preview assets with their licenses and retained the root `RECIPE.md` and `preview.png`.
- **Change**: Updated documentation links, helper instructions, preview script paths, and preview asset manifest paths for the new location.
- **Question**: What is the final schema and submission contract for the [file-grouping proposal](/agent-file-grouping-plan.md)? The example remains illustrative; grouping is not implemented.
