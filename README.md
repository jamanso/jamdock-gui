# jamdock-gui

Graphical interface for [jamdock-suite](https://github.com/jamanso/jamdock-suite) — an end-to-end pipeline for **automated virtual screening** built around **QuickVina 2**.

This GUI is a layer on top of the original bash scripts: it doesn't replace them, it makes them point-and-click. CLI users can keep using the scripts as-is.

## Pipeline covered

```
Library Generation    Receptor Preparation     Docking            Ranking
──────────────────    ────────────────────     ───────            ───────
ZINC tranches /  →    PDB → cleanup → PDBQT  → qvina02 batch  →   Top hits
FDA catalog           Fpocket pockets          (parallel)         (Affinity +
→ PDBQT library       → grid.conf              → docking_results  SimScore)
(jamlib)              (jamreceptor)            (jamqvina)         (jamrank)
```

## What's new in jamdock-gui (vs. the jamdock-suite CLI)

This GUI is more than a button-wrapper for the bash scripts. It adds capabilities that were impractical to bolt onto the original CLI:

- **Full graphical environment** — PySide6 desktop application with four task-oriented tabs (Library, Receptor, Docking, Results), persistent settings, a built-in dependency checker, and an integrated log console. CLI users can keep using the scripts as-is; nothing in the suite is removed.
- **Structural-water handling** — `jamreceptor` strips every water by default. The GUI ships a dedicated module (`core.waters`) that **detects** crystallographic waters in the input PDB, **scores** each one against four cheap structural criteria (B-factor, contact count, distance to pocket centroid, H-bond geometry), **filters** by user-tunable thresholds, and **injects** the surviving "bridge" waters back into the cleaned PDB so they participate in the docking. This typically rescues 1–2 kcal/mol of binding free energy that pure dry docking misses.
- **Titratable-residue protonation at user-selected pH** — optional PDB2PQR + PROPKA stage that adjusts the protonation states of HIS, ASP, GLU, CYS, LYS, ARG and TYR for the experimental pH. The GUI takes care of renaming the non-standard residue codes that PDB2PQR emits (HIE/HID/HIP, ASH, GLH…) back to the 3-letter codes that `prepare_receptor4` expects, so the workflow stays seamless.
- **Click-to-pick PyMOL viewer everywhere** — fully integrated PyMOL panel that you can drive with one click from any tab:
  - **Protein view**: chains and ligands are shown side by side; click a chain to keep it, click a pocket detected by Fpocket to use it as the binding site for the grid.
  - **Pocket preview**: every Fpocket cavity is rendered with its surface so you can compare them visually before choosing one.
  - **Pose inspection**: click any row in the Results table and the receptor + that exact pose load into PyMOL ready for inspection.
- **CPU parallelization of QuickVina jobs** — the Python orchestrator that replaces `jamqvina` runs `qvina02` in a configurable worker pool sized to the host's CPU count. Live throughput chart, accurate ETA, pause/resume, and crash recovery (so the old `jamresume` is no longer needed) deliver a measured **3–4× speed-up** vs. the serial bash version on multi-core machines.
- **Live, in-place results analysis** — as each docking job finishes, its results stream into the Results table without waiting for the batch to end. Each row's Lipinski Rule-of-Five status (MW, Crippen LogP, HBD/HBA, computed via RDKit) is evaluated on the fly and the row is **coloured green when all four criteria are met**, so druggable hits jump out of the list while the rest of the screening is still running. Filterable by Affinity, SimScore, MW or ZINC ID; one-click exports to CSV, XLSX, ZIP-of-poses, or a Markdown lab notebook.

## Installation

`jamdock-gui` is a Python layer on top of the **jamdock-suite** bash scripts (`jamlib`, `jamreceptor`, `jamqvina`, `jamrank`, `jamresume`) and the external binaries they orchestrate (`qvina02`, `fpocket`, MGLTools, OpenBabel). Installation is a two-step process: first set up jamdock-suite (which already documents how to install every external dependency), then install this GUI on top.

> **Supported platform.** v1.0 is tested on Linux (Ubuntu 22.04 / 24.04) and on WSL2. Native Windows and macOS are not officially supported in this release.

### Step 1 — Install jamdock-suite (one-time setup)

Follow the instructions at <https://github.com/jamanso/jamdock-suite> to install the base pipeline. That repository covers everything `jamdock-gui` depends on at runtime:

- `jamlib`, `jamreceptor`, `jamqvina`, `jamrank`, `jamresume` — the bash scripts that do the actual work.
- `qvina02`, `fpocket`, MGLTools (`prepare_ligand4.py`, `prepare_receptor4.py`), OpenBabel.

Verify the suite is on your `$PATH` before continuing:

```bash
which jamlib jamreceptor jamqvina jamrank jamresume
which qvina02 fpocket obabel
```

If any of these is missing, fix that first — the GUI will refuse to launch otherwise (and will tell you exactly which one it cannot find, in the **Dependencies** panel of the welcome screen).

### Step 2 — Install jamdock-gui

Once jamdock-suite is in place, install the GUI from PyPI:

```bash
pip install jamdock-gui
```

Or, to install the latest development version directly from GitHub:

```bash
pip install git+https://github.com/jamanso/jamdock-gui.git
```

We strongly recommend a dedicated virtual environment to keep Qt and RDKit isolated from your system Python:

```bash
python -m venv ~/.venvs/jamdock
source ~/.venvs/jamdock/bin/activate
pip install jamdock-gui
```

### Step 3 — Launch

```bash
jamdock-gui
```

The GUI auto-detects all binaries on launch. If something is on a non-standard path, open **Settings → Binary paths** and point it manually — the choices are persisted across sessions.

### WSL2 users

Everything works out of the box on WSL2 with WSLg (Windows 11 or recent Windows 10 builds), no X server required. If you are on an older Windows that needs an X server (VcXsrv, X410), launch it before starting `jamdock-gui` and make sure `DISPLAY` is exported in your shell.

## Usage

```bash
jamdock-gui
```

## Citation

If you use `jamdock-gui` or its outputs in publications, please cite the **method paper** for the pipeline:

- Barbosa Pereira, P.J., Ripoll-Rozada, J., Macedo-Ribeiro, S., & Manso, J.A. (2025). Protocol for an automated virtual screening pipeline including library generation and docking evaluation. *STAR Protocols* **6**(4), 104161. https://doi.org/10.1016/j.xpro.2025.104161

and the **software**:

- Manso, J.A. (2025). *jamdock-suite*. Zenodo. https://doi.org/10.5281/zenodo.15577778

Please also acknowledge the third-party tools the pipeline orchestrates:

- Trott, O., & Olson, A.J. (2010). AutoDock Vina. *J Comput Chem* **31**, 455–461.
- Alhossary, A. *et al.* (2015). QuickVina 2. *Bioinformatics* **31**, 2214–2216.
- Le Guilloux, V. *et al.* (2009). Fpocket. *BMC Bioinformatics* **10**, 168.
- Morris, G.M. *et al.* (2009). AutoDock4 / AutoDockTools. *J Comput Chem* **30**, 2785–2791.
- O'Boyle, N.M. *et al.* (2011). Open Babel. *J Cheminformatics* **3**, 33.
- Sterling, T. & Irwin, J.J. (2015). ZINC 15. *J Chem Inf Model* **55**, 2324–2337.

## License

Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0). Same as jamdock-suite.
