# Changelog

All notable changes to **jamdock-gui** are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-05-18

First public release.

### Headline features (vs. the jamdock-suite CLI)

- **Full graphical environment** — PySide6 desktop application that turns
  the entire jamdock-suite workflow into four task-oriented tabs
  (Library, Receptor, Docking, Results). Includes persistent settings,
  a built-in dependency checker, and an integrated log console. The CLI
  scripts remain usable on their own; nothing in the suite is removed.
- **Structural-water handling** — `jamreceptor` strips every water by
  default. The GUI ships a dedicated module (`core.waters`) that
  **detects** crystallographic waters in the input PDB, **scores** each
  one against four cheap structural criteria (B-factor, contact count,
  distance to the pocket centroid, H-bond geometry), **filters** by
  user-tunable thresholds, and **injects** the surviving "bridge"
  waters back into the cleaned PDB so they participate in the docking.
  This typically rescues 1–2 kcal/mol of binding free energy that pure
  dry docking misses.
- **Titratable-residue protonation at user-selected pH** — optional
  PDB2PQR + PROPKA stage that adjusts the protonation of HIS, ASP,
  GLU, CYS, LYS, ARG and TYR for the experimental pH. The GUI handles
  the non-standard residue codes that PDB2PQR emits (HIE/HID/HIP, ASH,
  GLH…) by renaming them back to the 3-letter codes that
  `prepare_receptor4` expects, so the workflow stays seamless end to
  end.
- **Click-to-pick PyMOL viewer everywhere** — fully integrated PyMOL
  panel driven from any tab:
  - *Protein view*: chains and ligands are shown side by side; click a
    chain to keep it, click a pocket detected by Fpocket to use it as
    the binding site for the grid.
  - *Pocket preview*: every Fpocket cavity is rendered with its surface
    so you can compare them visually before choosing one.
  - *Pose inspection*: click any row in the Results table and the
    receptor + that exact pose load into PyMOL ready for inspection.
- **CPU parallelization of QuickVina jobs** — the Python orchestrator
  that replaces `jamqvina` runs `qvina02` in a configurable worker pool
  sized to the host's CPU count. Live throughput chart, accurate ETA,
  pause / resume, and crash recovery (so the old `jamresume` is no
  longer needed) deliver a measured **3–4× speed-up** vs. the serial
  bash version on multi-core machines.
- **Live, in-place results analysis with Rule-of-Five colouring** — as
  each docking job finishes, its row streams into the Results table
  without waiting for the batch to end. Each row's Lipinski
  Rule-of-Five status (MW, Crippen LogP, HBD/HBA, computed via RDKit)
  is evaluated on the fly and the row is **coloured green when all
  four criteria are met**, so druggable hits jump out of the list
  while the rest of the screening is still running.

### Other additions
- Library Generation tab — point-and-click frontend for `jamlib`, with
  MW / LogP / N input controls, ZINC tranche selection, and live
  progress.
- Results table is filterable by Affinity, SimScore, MW or ZINC ID,
  with histograms, scatter plots, and one-click exports to CSV, XLSX,
  ZIP of poses, or a Markdown lab notebook (replaces `jamrank`).
- Settings dialog with persistent binary-path overrides and a
  dependency-status panel.
- WSL2 support — robust URL opener for the *Open ZINC link* action with
  multi-backend fallback (`cmd.exe /c start` → `rundll32` → `wslview` →
  `explorer.exe`).

### Fixed
- Silenced RDKit's `rdApp.*` log channel so SDFs with malformed
  quaternary-nitrogen valences no longer spam the terminal at startup.
- Suppressed `gio: Operation not supported` stderr noise leaked by
  `webbrowser.open` on Linux desktops with broken default-handler
  registration.

### Notes
- v1.0 is tested on Linux (Ubuntu 22.04 / 24.04) and on WSL2. Native
  Windows and macOS are not officially supported in this release.
- Depends on **jamdock-suite** being installed and on `$PATH`; see the
  README for the two-step install procedure.

[1.0.0]: https://github.com/jamanso/jamdock-gui/releases/tag/v1.0.0
