---
type: Playbook
title: "Gallery preview"
description: "Rebuild and safely capture the synthetic native preview and distribution artwork."
tags: ["distribution", "preview", "privacy"]
generated: { by: "pi/1.5.2", at: "2026-11-21T23:18:23Z" }
sources:
  - resource: "Repository worktree: mods/Codewalker/pr-client.js, pr-model.js, pull-requests.js, main.js, client.js, backend.py, scripts/preview.py"
    title: "Current-branch PR comparison implementation and synthetic preview"
  - resource: "Git revision febd5e0a69ca81d65d9ea3c0f593af824532c7d1, file docs/preview.md"
    title: "Original preview.md before OKF migration"
---
# Gallery preview

`../preview.png` is a 2560×1280 composition based on the user's reference: marketing title/subtitle on the left and the most useful native UI on the right. Codewalker shows the current PR’s Changes tab, with its viewed checklist and inline human–agent conversation. Description has its own full-height scroll tab. The background has blue/purple tints and a notebook-style dot grid; the native screenshot keeps its original colors. Text and artwork render at 2× resolution, not by enlarging the previous PNG.

The kept screenshot is [`preview-assets/review.png`](preview-assets/review.png). It is a genuine native Herdr render of the production `Codewalker` view, with synthetic demo content from [`fixture.js`](preview-assets/fixture.js). No private checkout, agent transcript, or surrounding window content is included. Cropping, downscaling, a rounded mask, shadow, background, and external marketing text are the only composition operations. UI controls and code were not redrawn or generated.

## Rebuild the artwork

Requires Python 3 and Pillow **for artwork only**. The installed mod uses no Pillow or other Python dependencies.

```sh
python3 scripts/preview.py --compose
```

The composition uses the bundled Inter 4.1 variable font from the upstream `v4.1` tag:

- https://github.com/rsms/inter/blob/v4.1/docs/font-files/InterVariable.ttf
- License: [`preview-assets/INTER-LICENSE.txt`](preview-assets/INTER-LICENSE.txt), SIL Open Font License 1.1.

All inputs are local; composition does not fetch fonts or imagery. The font and screenshot are not installed into the runtime mod.

## Native capture

The synthetic subclass renders actual Codewalker components. It overrides repository reads, folder following, writes, draft persistence, sends, and agent enumeration. No backend is included in the temporary fixture. Example paths, authors, messages, SHA, and code are invented demonstration data, not real review evidence.

1. Reserve the shared Herdr display before opening an overlay. The hidden check does not need a live capture slot:

   ```sh
   python3 scripts/preview.py --check --scene pr
   python3 scripts/preview.py --open --scene pr
   herdr-app shot /tmp/codewalker-native-window.png
   ```

   `--scene pr` shows Changes and matches the gallery screenshot; `--scene pr-description` shows Description, and `--scene pr-long-description` supplies a long scrollable description; `--scene no-pr` checks the **No open pull request** state. The default `commit` scene remains available. These scenes use synthetic state and never call GitHub.

   `--open` creates only the unused `mods/Codewalker-Distribution-Preview/` in the effective `HERDR_APP_HOME`. It refuses an occupied name and changes no real mod, layout, settings, keys, tabs, or terminal. It opens a 900×620 logical-pixel window by default, without pointer/key automation. Use `--width 400 --height 760 --scene pr` to inspect the stacked, vertically resizable PR Changes view.

2. Inspect the full capture **outside the repository**. It may contain private information around the demo. Confirm the preview is listed in `herdr-app status`, then toggle that exact mod closed:

   ```sh
   herdr-app do mod.open --name Codewalker-Distribution-Preview
   herdr-app status
   python3 scripts/preview.py --clean
   ```

   Do not use an implicit close command that could close someone else's overlay. `--clean` refuses an open or unowned preview folder. Release the display slot when the overlay is closed.

3. Crop only inside the demo into the kept source image. The current 3840×2098 capture used `X=1024 Y=434 WIDTH=1792 HEIGHT=1232`, safely inside the native frame. These coordinates depend on window size and zoom: **inspect a fresh capture before choosing them**, never reuse them blindly.

   ```sh
   python3 scripts/preview.py --crop /tmp/codewalker-native-window.png \
     --rect 1024 434 1792 1232
   python3 scripts/preview.py --compose
   ```

4. Inspect both the sanitized crop and the composed image. Only those images belong in the repository. Keep the original full-window screenshot private.

The source was captured with GPUI shell 0.7.1 and the host's dark theme at zoom 1. The fixture demonstrates layout/paint with synthetic data; it does not prove live editor, persistence, or delivery interactions. Those have separate behavior tests and manual checks.
