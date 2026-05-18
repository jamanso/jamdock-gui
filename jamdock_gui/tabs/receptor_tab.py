"""Tab 2 — Receptor Preparation. Replaces ``jamreceptor`` with a 4-step wizard.

The original bash logic is reproduced via Python helpers in :mod:`core`:

* ``core.pdb_clean``   — Step 1 (awk port)
* ``core.receptor_prep`` (PrepareReceptorRunner)  — Step 2 (MGLTools)
* ``core.receptor_prep`` (FpocketRunner) + ``core.pocket`` — Step 3
* ``core.grid_box``    — Step 4

Each step writes its outputs into the working directory. Re-running a step
invalidates downstream steps so the user can iterate.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jamdock_gui.core.grid_box import DEFAULT_PADDING, GridBox
from jamdock_gui.core.pdb_clean import ChainInfo, clean_pdb, detect_chains
from jamdock_gui.core.pocket import (
    DRUGGABLE_THRESHOLD,
    Pocket,
    parse_pocket_info,
    pocket_files,
    read_pocket_atoms,
)
from jamdock_gui.core.pymol_launcher import (
    find_pymol,
    launch_pymol,
    open_receptor_with_pockets_and_grid,
)
from jamdock_gui.core.receptor_prep import (
    FpocketRunner,
    Pdb2pqrOptions,
    Pdb2pqrRunner,
    PrepareReceptorOptions,
    PrepareReceptorRunner,
    expected_fpocket_dir,
    normalize_residue_names,
)
from jamdock_gui.deps import probe_all
from jamdock_gui.settings import Settings
from jamdock_gui.tabs.base_tab import BaseTab
from jamdock_gui.widgets.citation_dialog import (
    JAMRECEPTOR_CITATIONS_HTML,
    CitationDialog,
)
from jamdock_gui.core.waters import inject_waters_into_pdb
from jamdock_gui.widgets.log_console import LogConsole
from jamdock_gui.widgets.pymol_panel import PymolPanel
from jamdock_gui.widgets.water_panel import WaterPanel


STEPS: tuple[str, ...] = (
    "Step 1 — Load & Clean PDB",
    "Step 2 — Convert to PDBQT",
    "Step 3 — Detect Pockets (Fpocket)",
    "Step 4 — Define Grid Box",
)


# ===========================================================================
# Step 1 — Load & Clean PDB
# ===========================================================================
class Step1Widget(QWidget):
    """File picker + chain table + Run Clean."""

    # Emitted when cleaning succeeds: (cleaned_pdb_path, chains_kept)
    def __init__(self, parent: "ReceptorTab") -> None:
        super().__init__(parent)
        self._tab = parent
        self._chain_items: list[tuple[QCheckBox, ChainInfo]] = []
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        # -- file row -------------------------------------------------
        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("<b>PDB file:</b>"))
        self.le_pdb = QLineEdit()
        self.le_pdb.setPlaceholderText("Select a .pdb file…")
        self.btn_browse = QPushButton("Browse…")
        self.btn_browse.clicked.connect(self._on_browse)
        file_row.addWidget(self.le_pdb, 1)
        file_row.addWidget(self.btn_browse)
        v.addLayout(file_row)

        # -- chains list ---------------------------------------------
        chains_box = QGroupBox("Chains detected — pick those to keep")
        cv = QVBoxLayout(chains_box)
        self.lst_chains = QListWidget()
        self.lst_chains.setMaximumHeight(170)
        cv.addWidget(self.lst_chains)
        self.lbl_chains_help = QLabel(
            "<i>Tip: protein-like chains are pre-checked. "
            "Het ligands and waters are stripped automatically.</i>"
        )
        self.lbl_chains_help.setWordWrap(True)
        cv.addWidget(self.lbl_chains_help)
        v.addWidget(chains_box)

        # -- structural waters panel ---------------------------------
        self.water_panel = WaterPanel()
        self.water_panel.selection_changed.connect(self._on_waters_changed)
        v.addWidget(self.water_panel)

        # -- run row --------------------------------------------------
        run_row = QHBoxLayout()
        self.btn_clean = QPushButton("▶  Clean PDB")
        self.btn_clean.setEnabled(False)
        self.btn_clean.clicked.connect(self._on_clean)
        self.btn_view_pymol = QPushButton("View in PyMOL")
        self.btn_view_pymol.setEnabled(False)
        self.btn_view_pymol.clicked.connect(self._on_view_pymol)
        run_row.addWidget(self.btn_clean)
        run_row.addWidget(self.btn_view_pymol)
        self.lbl_status = QLabel("")
        run_row.addWidget(self.lbl_status, 1)
        v.addLayout(run_row)

        v.addStretch(1)

    def _on_view_pymol(self) -> None:
        path_str = self.le_pdb.text().strip()
        if not path_str:
            return
        pymol = find_pymol()
        if not pymol:
            QMessageBox.warning(self, "PyMOL not found",
                "PyMOL is not on PATH. Install PyMOL to enable external viewing.")
            return
        ok, msg = launch_pymol(pymol=pymol, files=[Path(path_str)],
                               workdir=self._tab.workdir)
        if ok:
            self._tab.append_log_info(f"PyMOL launched: {msg}")
        else:
            self._tab.append_log_info(f"PyMOL launch FAILED: {msg}")
            QMessageBox.warning(self, "PyMOL launch failed", msg)

    # ------------------------------------------------------------------
    def _on_browse(self) -> None:
        start = self._tab.workdir or Path.home()
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Select receptor PDB", str(start),
            "PDB files (*.pdb *.ent);;All files (*)"
        )
        if chosen:
            self.set_pdb(Path(chosen))

    def set_pdb(self, path: Path) -> None:
        self.le_pdb.setText(str(path))
        self._populate_chains(path)
        self.btn_clean.setEnabled(True)
        self.btn_view_pymol.setEnabled(find_pymol() is not None)
        self._tab.append_log_info(f"Loaded {path.name}")
        # Trigger structural-water detection on the source PDB.
        kept = set(self.selected_chains()) or None
        self.water_panel.set_pdb(path, chains_kept=kept)
        if self.water_panel.has_waters:
            self._tab.append_log_info(
                f"  {len(self.water_panel._waters)} waters detected — see "
                "the Structural waters panel to review."
            )

    def _on_waters_changed(self) -> None:
        # Just refresh log; injection happens at Clean time.
        kept = self.water_panel.kept_waters()
        if kept:
            self._tab.append_log_info(
                f"  Will preserve {len(kept)} water(s) when cleaning: "
                + ", ".join(w.label for w in kept[:8])
                + (" …" if len(kept) > 8 else "")
            )

    def _populate_chains(self, pdb: Path) -> None:
        self.lst_chains.clear()
        self._chain_items.clear()
        try:
            chains = detect_chains(pdb)
        except Exception as exc:
            QMessageBox.critical(self, "Failed to read PDB", f"{exc}")
            return
        if not chains:
            QMessageBox.warning(
                self, "No chains found",
                "No ATOM/HETATM lines were found in the file."
            )
            return
        for c in chains:
            text = (
                f"  Chain {c.chain_id or '·'}   "
                f"{c.n_residues} residues, {c.n_atoms} atoms"
                + ("   ✓ protein" if c.is_protein else "")
                + ("   ✦ has HETATM" if c.has_hetatm else "")
            )
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if c.is_protein else Qt.Unchecked)
            item.setData(Qt.UserRole, c.chain_id)
            self.lst_chains.addItem(item)

    def selected_chains(self) -> list[str]:
        out: list[str] = []
        for row in range(self.lst_chains.count()):
            item = self.lst_chains.item(row)
            if item.checkState() == Qt.Checked:
                cid = item.data(Qt.UserRole)
                if cid is not None:
                    out.append(str(cid))
        return out

    def _on_clean(self) -> None:
        if not self._tab.workdir:
            QMessageBox.warning(
                self, "Working directory required",
                "Please choose a working directory at the top of the window."
            )
            return
        pdb_str = self.le_pdb.text().strip()
        if not pdb_str:
            QMessageBox.warning(self, "No PDB", "Pick a PDB file first.")
            return
        src = Path(pdb_str)
        if not src.is_file():
            QMessageBox.warning(self, "PDB not found", f"{src} does not exist.")
            return
        chains = self.selected_chains()
        if not chains:
            QMessageBox.warning(
                self, "Pick at least one chain",
                "Tick the chains you want to keep."
            )
            return

        out = self._tab.workdir / (src.stem + "_for_docking.pdb")
        try:
            report = clean_pdb(src, out, chains_to_keep=chains)
        except (ValueError, FileNotFoundError) as exc:
            QMessageBox.critical(self, "Clean failed", str(exc))
            return

        self.lbl_status.setText(
            f"✓ Kept {report.n_atom_lines_kept} atoms "
            f"({report.n_residues_kept} residues) → {out.name}"
        )
        self._tab.append_log_info(
            f"✓ Cleaned PDB written: {out.name}  "
            f"({report.n_atom_lines_kept} atoms, "
            f"{report.n_residues_kept} residues, chains {chains})"
        )

        # Inject preserved structural waters, if any.
        kept_waters = self.water_panel.kept_waters()
        if kept_waters:
            try:
                inject_waters_into_pdb(out, kept_waters, output_pdb=out)
                self._tab.append_log_info(
                    f"  + {len(kept_waters)} structural water(s) preserved: "
                    + ", ".join(w.label for w in kept_waters[:8])
                    + (" ..." if len(kept_waters) > 8 else "")
                )
            except (FileNotFoundError, OSError) as exc:
                self._tab.append_log_info(f"  water injection failed: {exc}")
        # Propagate state to the tab so Step 2 can adapt its -U flag.
        self._tab.kept_waters = list(kept_waters)

        # Notify the tab - advances the wizard state.
        self._tab.on_step1_done(out, chains)


# ===========================================================================
# Step 2 — PDB → PDBQT via MGLTools
# ===========================================================================
class Step2Widget(QWidget):
    def __init__(self, parent: "ReceptorTab") -> None:
        super().__init__(parent)
        self._tab = parent
        self._runner: PrepareReceptorRunner | None = None
        self._pdb2pqr_runner: Pdb2pqrRunner | None = None
        # Carries the path of the protonated PDB between the two stages.
        self._pending_pdb_for_prep: Path | None = None
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        s = self._tab.settings

        self.le_pythonsh = QLineEdit(s.get_binary(Settings.KEY_PYTHONSH) or "")
        self.le_pythonsh.setPlaceholderText("Path to MGLTools' pythonsh")
        btn_browse_pythonsh = QPushButton("Browse…")
        btn_browse_pythonsh.clicked.connect(
            lambda: self._browse_into(self.le_pythonsh, "pythonsh", "")
        )
        row1 = QHBoxLayout()
        row1.addWidget(self.le_pythonsh, 1)
        row1.addWidget(btn_browse_pythonsh)
        form.addRow("pythonsh:", _wrap(row1))

        self.le_prep = QLineEdit(s.get_binary(Settings.KEY_PREP_RECEPTOR) or "")
        self.le_prep.setPlaceholderText("Path to prepare_receptor4.py")
        btn_browse_prep = QPushButton("Browse…")
        btn_browse_prep.clicked.connect(
            lambda: self._browse_into(self.le_prep, "prepare_receptor4.py", "*.py")
        )
        row2 = QHBoxLayout()
        row2.addWidget(self.le_prep, 1)
        row2.addWidget(btn_browse_prep)
        form.addRow("prepare_receptor4.py:", _wrap(row2))
        v.addLayout(form)

        opts_box = QGroupBox("Options")
        ov = QVBoxLayout(opts_box)
        self.cb_addH = QCheckBox("Add hydrogens (-A hydrogens)")
        self.cb_addH.setChecked(True)
        self.cb_clean = QCheckBox(
            "Remove non-polar H, lone pairs, waters, non-std residues "
            "(-U nphs_lps_waters_nonstdres)"
        )
        self.cb_clean.setChecked(True)
        self.cb_verbose = QCheckBox("Verbose (-v)")
        self.cb_verbose.setChecked(True)
        ov.addWidget(self.cb_addH)
        ov.addWidget(self.cb_clean)
        ov.addWidget(self.cb_verbose)
        v.addWidget(opts_box)

        # ---- Protonation block (PDB2PQR) ------------------------------
        proton_box = QGroupBox("Protonation states by pH (PDB2PQR)")
        pl = QVBoxLayout(proton_box)
        self.cb_protonate = QCheckBox(
            "Adjust protonation of titratable residues (HIS, ASP, GLU, "
            "LYS, CYS, TYR) for the chosen pH using PDB2PQR"
        )
        self.cb_protonate.setChecked(False)
        self.cb_protonate.toggled.connect(self._on_protonate_toggled)
        pl.addWidget(self.cb_protonate)

        proton_row = QHBoxLayout()
        proton_row.addWidget(QLabel("pH:"))
        self.sp_ph = QDoubleSpinBox()
        self.sp_ph.setDecimals(1)
        self.sp_ph.setRange(0.0, 14.0)
        self.sp_ph.setSingleStep(0.1)
        self.sp_ph.setValue(7.4)
        self.sp_ph.setEnabled(False)
        proton_row.addWidget(self.sp_ph)
        proton_row.addSpacing(12)
        proton_row.addWidget(QLabel("Force field:"))
        self.cb_ff = QComboBox()
        self.cb_ff.addItems(["PARSE", "AMBER", "CHARMM"])
        self.cb_ff.setEnabled(False)
        proton_row.addWidget(self.cb_ff)
        proton_row.addStretch(1)
        pl.addLayout(proton_row)

        self.cb_propka = QCheckBox(
            "Use PROPKA for residue-specific pKa shifts (recommended)"
        )
        self.cb_propka.setChecked(True)
        self.cb_propka.setEnabled(False)
        pl.addWidget(self.cb_propka)

        hint = QLabel(
            "<i>When enabled: PDB2PQR is run first, residue names are "
            "normalised back to ASP/GLU/HIS… for MGLTools, then "
            "prepare_receptor4 keeps the H atoms placed at the requested "
            "pH (it won't re-add hydrogens).</i>"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("QLabel { color: #555; padding: 4px; }")
        pl.addWidget(hint)
        v.addWidget(proton_box)
        # ---- end Protonation block ------------------------------------

        run_row = QHBoxLayout()
        self.btn_run = QPushButton("▶  Run prepare_receptor4")
        self.btn_run.setEnabled(False)
        self.btn_run.clicked.connect(self._on_run)
        self.lbl_status = QLabel("")
        run_row.addWidget(self.btn_run)
        run_row.addWidget(self.lbl_status, 1)
        v.addLayout(run_row)

        v.addStretch(1)

    def _on_protonate_toggled(self, checked: bool) -> None:
        self.sp_ph.setEnabled(checked)
        self.cb_ff.setEnabled(checked)
        self.cb_propka.setEnabled(checked)
        # When protonation is on, prepare_receptor4 must NOT re-add H.
        # Force the checkbox off and disable it so the user can't undo it.
        if checked:
            self.cb_addH.setChecked(False)
            self.cb_addH.setEnabled(False)
            self.cb_addH.setToolTip(
                "Disabled while PDB2PQR is on — pdb2pqr already places H "
                "at the requested pH; -A hydrogens would override that."
            )
            self.btn_run.setText("▶  Run PDB2PQR + prepare_receptor4")
        else:
            self.cb_addH.setEnabled(True)
            self.cb_addH.setChecked(True)
            self.cb_addH.setToolTip("")
            self.btn_run.setText("▶  Run prepare_receptor4")

    def _browse_into(self, line_edit: QLineEdit, title: str, name_filter: str) -> None:
        start = line_edit.text() or str(Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self, f"Select {title}", start,
            name_filter or "All files (*)"
        )
        if chosen:
            line_edit.setText(chosen)

    def enable_run(self, ok: bool) -> None:
        self.btn_run.setEnabled(ok)

    def _on_run(self) -> None:
        cleaned = self._tab.cleaned_pdb
        if cleaned is None or not cleaned.is_file():
            QMessageBox.warning(self, "Step 1 required",
                                "Run Step 1 (Clean PDB) first.")
            return
        pythonsh = Path(self.le_pythonsh.text().strip() or "")
        prep = Path(self.le_prep.text().strip() or "")
        if not pythonsh.is_file():
            QMessageBox.warning(self, "pythonsh not found",
                                "Pick the pythonsh binary from your MGLTools install.")
            return
        if not prep.is_file():
            QMessageBox.warning(self, "prepare_receptor4.py not found",
                                "Pick prepare_receptor4.py inside MGLTools/AutoDockTools/Utilities24/.")
            return

        # Persist for next time.
        s = self._tab.settings
        s.set_binary(Settings.KEY_PYTHONSH, str(pythonsh))
        s.set_binary(Settings.KEY_PREP_RECEPTOR, str(prep))

        # Decide whether to chain through PDB2PQR or go straight to prepare_receptor4.
        if self.cb_protonate.isChecked():
            self._run_pdb2pqr_then_prep(cleaned, pythonsh, prep)
        else:
            self._run_prepare_receptor4(cleaned, pythonsh, prep)

    def _run_pdb2pqr_then_prep(
        self, cleaned: Path, pythonsh: Path, prep: Path,
    ) -> None:
        """Stage 1: pdb2pqr → normalise → stage 2: prepare_receptor4."""
        statuses = probe_all(self._tab.settings)
        st = statuses.get("pdb2pqr")
        if not st or not st.found:
            QMessageBox.warning(
                self, "pdb2pqr not found",
                "Install pdb2pqr to use pH-dependent protonation:\n"
                "  pip install pdb2pqr"
            )
            return

        ph = self.sp_ph.value()
        ff = self.cb_ff.currentText()
        protonated = self._tab.workdir / f"{cleaned.stem}_pH{ph:g}.pdb"
        opts = Pdb2pqrOptions(
            ph=ph, forcefield=ff,
            use_propka=self.cb_propka.isChecked(),
            keep_chain=True,
        )

        runner = Pdb2pqrRunner(self)
        runner.line.connect(self._tab.log.append_stdout)
        runner.err_line.connect(self._tab.log.append_stderr)
        runner.failed_to_start.connect(self._on_failed)
        runner.finished.connect(
            lambda code, _st, p=protonated, py=pythonsh, pr=prep:
            self._on_pdb2pqr_finished(code, p, py, pr)
        )
        self._pdb2pqr_runner = runner

        # Save what we'll use in stage 2.
        self._stage2_pythonsh = pythonsh
        self._stage2_prep = prep
        self._stage2_protonated = protonated

        self.btn_run.setEnabled(False)
        self.lbl_status.setText(f"Running pdb2pqr at pH {ph:g}…")
        self._tab.append_log_info(
            f"▶ pdb2pqr  ff={ff}  pH={ph:g}  → {protonated.name}"
        )
        try:
            runner.start(
                pdb2pqr=Path(st.path or "pdb2pqr30"),
                input_pdb=cleaned,
                output_pdb=protonated,
                workdir=self._tab.workdir,
                options=opts,
            )
        except RuntimeError as exc:
            self.lbl_status.setText(f"Error: {exc}")
            self.btn_run.setEnabled(True)

    def _on_pdb2pqr_finished(
        self, exit_code: int, protonated: Path,
        pythonsh: Path, prep: Path,
    ) -> None:
        if exit_code != 0 or not protonated.is_file():
            self.lbl_status.setText(f"✗ pdb2pqr exit {exit_code}")
            self._tab.append_log_info(
                f"✗ pdb2pqr finished with exit code {exit_code}"
            )
            self.btn_run.setEnabled(True)
            return

        # Normalise residue names so MGLTools recognises them.
        normalized = protonated.with_name(protonated.stem + "_norm.pdb")
        try:
            counts = normalize_residue_names(protonated, normalized)
        except OSError as exc:
            QMessageBox.critical(self, "Residue rename failed", str(exc))
            self.btn_run.setEnabled(True)
            return

        if counts:
            summary = ", ".join(f"{n}× {k}" for k, n in sorted(counts.items()))
            self._tab.append_log_info(
                f"  Renamed protonation residues for MGLTools: {summary}"
            )
        else:
            self._tab.append_log_info(
                "  No alternative residue names emitted by pdb2pqr "
                "(structure is already standard)."
            )

        # Now stage 2: prepare_receptor4 on the normalised PDB.
        self._tab.append_log_info(
            "▶ Stage 2: prepare_receptor4 on the protonated structure"
        )
        self._run_prepare_receptor4(
            normalized, pythonsh, prep,
            override_addH=False,   # H atoms are already in place.
        )

    def _run_prepare_receptor4(
        self,
        input_pdb: Path,
        pythonsh: Path,
        prep: Path,
        *,
        override_addH: bool | None = None,
    ) -> None:
        """Run prepare_receptor4 on *input_pdb*, producing the receptor PDBQT.

        ``override_addH`` lets the chained-from-pdb2pqr path force ``-A`` off.
        """
        out_pdbqt = self._tab.workdir / (input_pdb.stem + ".pdbqt")
        add_h = self.cb_addH.isChecked() if override_addH is None else override_addH
        # If Step 1 preserved structural waters, do NOT pass `waters` to -U
        # or MGLTools would strip them again. Keep the rest of the cleanup.
        if self.cb_clean.isChecked():
            cleanup = ("nphs_lps_nonstdres"
                       if getattr(self._tab, "kept_waters", None)
                       else "nphs_lps_waters_nonstdres")
        else:
            cleanup = ""
        opts = PrepareReceptorOptions(
            add_hydrogens=add_h,
            cleanup=cleanup,
            verbose=self.cb_verbose.isChecked(),
        )
        if getattr(self._tab, "kept_waters", None):
            self._tab.append_log_info(
                f"  (preserving {len(self._tab.kept_waters)} structural "
                "water(s) — using cleanup flag without 'waters')"
            )

        runner = PrepareReceptorRunner(self)
        runner.line.connect(self._tab.log.append_stdout)
        runner.err_line.connect(self._tab.log.append_stderr)
        runner.failed_to_start.connect(self._on_failed)
        runner.finished.connect(lambda code, _st, p=out_pdbqt: self._on_finished(code, p))
        self._runner = runner
        self.btn_run.setEnabled(False)
        self.lbl_status.setText("Running prepare_receptor4…")
        self._tab.append_log_info(
            f"▶ prepare_receptor4 → {out_pdbqt.name}"
        )
        try:
            runner.start(
                pythonsh=pythonsh,
                prepare_receptor4=prep,
                cleaned_pdb=input_pdb,
                output_pdbqt=out_pdbqt,
                workdir=self._tab.workdir,
                options=opts,
            )
        except RuntimeError as exc:
            self.lbl_status.setText(f"Error: {exc}")
            self.btn_run.setEnabled(True)

    def _on_failed(self, msg: str) -> None:
        self.btn_run.setEnabled(True)
        self.lbl_status.setText("✗ Failed to start")
        QMessageBox.critical(self, "Failed to start", msg)

    def _on_finished(self, exit_code: int, out_pdbqt: Path) -> None:
        self.btn_run.setEnabled(True)
        if exit_code != 0:
            self.lbl_status.setText(f"✗ Exit {exit_code}")
            self._tab.append_log_info(
                f"✗ prepare_receptor4 finished with exit code {exit_code}"
            )
            return
        if not out_pdbqt.is_file():
            self.lbl_status.setText("✗ Output PDBQT missing")
            return
        self.lbl_status.setText(f"✓ {out_pdbqt.name}")
        self._tab.append_log_info(f"✓ Receptor PDBQT ready: {out_pdbqt.name}")
        self._tab.on_step2_done(out_pdbqt)


# ===========================================================================
# Step 3 — Fpocket
# ===========================================================================
class Step3Widget(QWidget):
    def __init__(self, parent: "ReceptorTab") -> None:
        super().__init__(parent)
        self._tab = parent
        self._runner: FpocketRunner | None = None
        self._pockets: list[Pocket] = []
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        run_row = QHBoxLayout()
        self.btn_run = QPushButton("▶  Detect pockets (Fpocket)")
        self.btn_run.setEnabled(False)
        self.btn_run.clicked.connect(self._on_run)
        self.btn_view_pockets = QPushButton("View pockets in PyMOL")
        self.btn_view_pockets.setEnabled(False)
        self.btn_view_pockets.setToolTip(
            "Open PyMOL with the receptor and the Fpocket pockets coloured. "
            "Useful for visually inspecting candidate pockets before ticking them."
        )
        self.btn_view_pockets.clicked.connect(self._on_view_pockets_pymol)
        self.lbl_status = QLabel("")
        run_row.addWidget(self.btn_run)
        run_row.addWidget(self.btn_view_pockets)
        run_row.addWidget(self.lbl_status, 1)
        v.addLayout(run_row)

        self.tbl = QTableWidget(0, 6)
        self.tbl.setHorizontalHeaderLabels(
            ["Use", "#", "Druggability", "Volume", "Hydrophobicity", "Druggable?"]
        )
        hdr = self.tbl.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.Stretch)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        v.addWidget(self.tbl, 1)

        hint = QLabel(
            "<i>Tick the pockets you want to use. Click "
            "<b>View pockets in PyMOL</b> to inspect them in 3D before "
            f"selecting; pockets with druggability &gt; {DRUGGABLE_THRESHOLD:g} "
            "are flagged with a star.</i>"
        )
        hint.setWordWrap(True)
        v.addWidget(hint)

    def enable_run(self, ok: bool) -> None:
        self.btn_run.setEnabled(ok)

    def _on_run(self) -> None:
        receptor = self._tab.receptor_pdbqt
        if receptor is None or not receptor.is_file():
            QMessageBox.warning(self, "Step 2 required",
                                "Convert the receptor to PDBQT first.")
            return
        statuses = probe_all(self._tab.settings)
        fpocket_st = statuses.get("fpocket")
        if not fpocket_st or not fpocket_st.found:
            QMessageBox.warning(
                self, "Fpocket not found",
                "Install Fpocket (https://github.com/Discngine/fpocket) "
                "and ensure it's on $PATH."
            )
            return

        runner = FpocketRunner(self)
        runner.line.connect(self._tab.log.append_stdout)
        runner.err_line.connect(self._tab.log.append_stderr)
        runner.failed_to_start.connect(self._on_failed)
        runner.finished.connect(self._on_finished)
        self._runner = runner

        self.btn_run.setEnabled(False)
        self.lbl_status.setText("Running…")
        self.tbl.setRowCount(0)
        self._tab.append_log_info(f"▶ fpocket -f {receptor.name}")

        try:
            runner.start(
                fpocket=Path(fpocket_st.path or "fpocket"),
                input_pdbqt=receptor,
                out_dir=None,                 # let fpocket pick <base>_out
                workdir=self._tab.workdir,
            )
        except RuntimeError as exc:
            self.lbl_status.setText(f"Error: {exc}")
            self.btn_run.setEnabled(True)

    def _on_failed(self, msg: str) -> None:
        self.btn_run.setEnabled(True)
        self.lbl_status.setText("✗ Failed to start")
        QMessageBox.critical(self, "Failed to start", msg)

    def _on_finished(self, exit_code: int, _exit_status: int) -> None:
        self.btn_run.setEnabled(True)
        if exit_code != 0:
            self.lbl_status.setText(f"✗ Exit {exit_code}")
            self._tab.append_log_info(
                f"✗ fpocket finished with exit code {exit_code}"
            )
            return

        recv = self._tab.receptor_pdbqt
        out_dir = expected_fpocket_dir(recv) if recv else None
        if not out_dir or not out_dir.is_dir():
            self.lbl_status.setText("✗ Fpocket output dir missing")
            return
        info_path, pockets_dir = pocket_files(out_dir, recv.stem)
        if not info_path.is_file():
            self.lbl_status.setText("✗ pocket info file missing")
            return

        self._pockets = parse_pocket_info(info_path)
        self._populate_table()

        # Build sphere bundles for the viewer (and stash in the tab so Step 4
        # can re-use them for the bounding box).
        bundles = []
        for p in self._pockets:
            atm_pdb = pockets_dir / f"pocket{p.number}_atm.pdb"
            if atm_pdb.is_file():
                atoms = read_pocket_atoms(atm_pdb)
                bundles.append({
                    "number": p.number,
                    "druggable": p.is_druggable,
                    "atoms": atoms,
                })
        self._tab.set_pocket_bundles(bundles)
        # Tell the sidebar PyMOL panel where Fpocket's .pml lives, so the
        # main "Open in PyMOL" button now loads receptor + pockets together.
        fpocket_pml = out_dir / f"{recv.stem}.pml"
        if fpocket_pml.is_file():
            self._tab.viewer.set_fpocket_pml(fpocket_pml)
            self._tab.viewer.set_pocket_spheres(bundles)
        self.btn_view_pockets.setEnabled(find_pymol() is not None)

        druggable = sum(1 for p in self._pockets if p.is_druggable)
        self.lbl_status.setText(
            f"✓ {len(self._pockets)} pockets   ({druggable} druggable)"
        )
        self._tab.append_log_info(
            f"✓ Fpocket: {len(self._pockets)} pockets, {druggable} druggable"
        )

    def _populate_table(self) -> None:
        self.tbl.setRowCount(len(self._pockets))
        for row, p in enumerate(self._pockets):
            cb = QCheckBox()
            cb.setChecked(p.is_druggable)
            cb.stateChanged.connect(lambda _state, n=p.number: self._tab.on_pocket_toggled())
            cell_w = QWidget()
            hl = QHBoxLayout(cell_w)
            hl.setContentsMargins(8, 0, 0, 0)
            hl.addWidget(cb)
            hl.addStretch(1)
            self.tbl.setCellWidget(row, 0, cell_w)
            cb.setProperty("pocket_number", p.number)

            self.tbl.setItem(row, 1, _table_num(p.number))
            self.tbl.setItem(row, 2, _table_float(p.druggability_score))
            self.tbl.setItem(row, 3, _table_float(p.volume))
            self.tbl.setItem(row, 4, _table_float(p.hydrophobicity_score))
            badge = QTableWidgetItem("★" if p.is_druggable else "")
            badge.setTextAlignment(Qt.AlignCenter)
            if p.is_druggable:
                badge.setForeground(QColor("#d4a017"))
            self.tbl.setItem(row, 5, badge)
        # Cache the checkboxes for fast retrieval
        # (we re-derive selected list on demand instead of caching state)

    def selected_pocket_numbers(self) -> list[int]:
        out: list[int] = []
        for row in range(self.tbl.rowCount()):
            cell = self.tbl.cellWidget(row, 0)
            if not cell:
                continue
            for child in cell.findChildren(QCheckBox):
                if child.isChecked():
                    n = child.property("pocket_number")
                    if n is not None:
                        out.append(int(n))
        return out

    def _on_view_pockets_pymol(self) -> None:
        """Launch PyMOL with the receptor + Fpocket-coloured pockets."""
        receptor = self._tab.receptor_pdbqt
        if receptor is None or not receptor.is_file():
            QMessageBox.warning(self, "Run Step 2 + Fpocket first",
                                "Need a receptor PDBQT and Fpocket output.")
            return
        pymol = find_pymol()
        if not pymol:
            QMessageBox.warning(self, "PyMOL not found",
                "PyMOL is not on PATH. Install it to use external 3D viewing.")
            return
        out_dir = receptor.with_name(receptor.stem + "_out")
        fpocket_pml = out_dir / f"{receptor.stem}.pml"

        # If Fpocket's .pml exists, load ONLY it — the .pml internally does
        # ``load receptor.pdb`` with paths relative to its own directory.
        # Passing the receptor on top would duplicate the structure and
        # confuse the relative loads.
        if fpocket_pml.is_file():
            files = [fpocket_pml]
            cwd = out_dir              # let the .pml's relative loads work
        else:
            files = [receptor]
            cwd = self._tab.workdir

        ok, msg = launch_pymol(pymol=pymol, files=files, workdir=cwd)
        if ok:
            self._tab.append_log_info(
                f"Launched PyMOL with "
                + (f"pockets: {fpocket_pml.name}" if fpocket_pml.is_file()
                   else f"receptor: {receptor.name}")
            )
        else:
            self._tab.append_log_info(f"PyMOL launch FAILED: {msg}")
            QMessageBox.warning(self, "PyMOL launch failed", msg)


def _table_num(n: int) -> QTableWidgetItem:
    item = QTableWidgetItem(str(n))
    item.setTextAlignment(Qt.AlignCenter)
    return item


def _table_float(x: float | None) -> QTableWidgetItem:
    text = "—" if x is None else f"{x:.3f}"
    item = QTableWidgetItem(text)
    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return item


# ===========================================================================
# Step 4 — Grid box
# ===========================================================================
class Step4Widget(QWidget):
    def __init__(self, parent: "ReceptorTab") -> None:
        super().__init__(parent)
        self._tab = parent
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        # padding slider (0..30 Å, integer steps; we display as float)
        pad_box = QGroupBox("Padding (Å) — total extra added per axis")
        pl = QHBoxLayout(pad_box)
        self.sld_padding = QSlider(Qt.Horizontal)
        self.sld_padding.setRange(0, 60)         # 0–30 Å in 0.5 Å steps
        self.sld_padding.setSingleStep(1)
        self.sld_padding.setPageStep(2)
        self.sld_padding.setValue(int(DEFAULT_PADDING * 2))
        self.sp_padding = QDoubleSpinBox()
        self.sp_padding.setDecimals(1)
        self.sp_padding.setRange(0.0, 30.0)
        self.sp_padding.setSingleStep(0.5)
        self.sp_padding.setValue(DEFAULT_PADDING)
        self.sld_padding.valueChanged.connect(self._on_slider_changed)
        self.sp_padding.valueChanged.connect(self._on_spin_changed)
        pl.addWidget(self.sld_padding, 1)
        pl.addWidget(self.sp_padding)
        v.addWidget(pad_box)

        # auto-computed values
        self.lbl_box = QLabel(self._format_box(None))
        self.lbl_box.setStyleSheet(
            "QLabel { font-family: monospace; padding: 6px;"
            " background: #f8f8f8; border: 1px solid #e0e0e0; }"
        )
        self.lbl_box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.lbl_box)

        # save buttons
        save_row = QHBoxLayout()
        self.btn_save_conf = QPushButton("Save grid.conf")
        self.btn_save_pml = QPushButton("Save grid_box.py (PyMOL)")
        self.btn_open_pymol = QPushButton("Open in PyMOL")
        self.btn_save_conf.setEnabled(False)
        self.btn_save_pml.setEnabled(False)
        self.btn_open_pymol.setEnabled(False)
        self.btn_save_conf.clicked.connect(self._on_save_conf)
        self.btn_save_pml.clicked.connect(self._on_save_pml)
        self.btn_open_pymol.clicked.connect(self._on_open_pymol)
        save_row.addWidget(self.btn_save_conf)
        save_row.addWidget(self.btn_save_pml)
        save_row.addWidget(self.btn_open_pymol)
        save_row.addStretch(1)
        v.addLayout(save_row)

        self.lbl_save_status = QLabel("")
        v.addWidget(self.lbl_save_status)
        v.addStretch(1)

        self._current_box: GridBox | None = None

    def _format_box(self, box: GridBox | None) -> str:
        if box is None:
            return "<i>Select pockets in Step 3 to compute the grid box.</i>"
        return (
            f"center_x = {box.center_x:.1f}    "
            f"center_y = {box.center_y:.1f}    "
            f"center_z = {box.center_z:.1f}\n"
            f"size_x   = {round(box.size_x):d}     "
            f"size_y   = {round(box.size_y):d}     "
            f"size_z   = {round(box.size_z):d}"
        )

    def padding(self) -> float:
        return self.sp_padding.value()

    def _on_slider_changed(self, value: int) -> None:
        # Block reciprocal signal to avoid double recompute
        self.sp_padding.blockSignals(True)
        self.sp_padding.setValue(value / 2.0)
        self.sp_padding.blockSignals(False)
        self.recompute()

    def _on_spin_changed(self, value: float) -> None:
        self.sld_padding.blockSignals(True)
        self.sld_padding.setValue(int(round(value * 2)))
        self.sld_padding.blockSignals(False)
        self.recompute()

    def recompute(self) -> None:
        atoms = self._tab.selected_pocket_atoms()
        if not atoms:
            self._current_box = None
            self.lbl_box.setText(self._format_box(None))
            self.btn_save_conf.setEnabled(False)
            self.btn_save_pml.setEnabled(False)
            self.btn_open_pymol.setEnabled(False)
            return
        try:
            box = GridBox.from_coords(atoms, padding=self.padding())
        except ValueError:
            self._current_box = None
            return
        self._current_box = box
        self.lbl_box.setText(self._format_box(box))
        self.btn_save_conf.setEnabled(True)
        self.btn_save_pml.setEnabled(True)
        # Open-in-PyMOL needs both the receptor PDBQT and PyMOL itself.
        self.btn_open_pymol.setEnabled(
            self._tab.receptor_pdbqt is not None
            and find_pymol() is not None
        )
        # If grid.conf / grid_box.py were previously saved, refresh them
        # in place so the latest padding propagates without a second click.
        wd = self._tab.workdir
        if wd:
            for name, content in (
                ("grid.conf", box.to_vina_conf()),
                ("grid_box.py", box.to_pymol_cgo()),
            ):
                path = wd / name
                if path.is_file():
                    try:
                        path.write_text(content, encoding="utf-8")
                    except OSError:
                        pass

    def _on_save_conf(self) -> None:
        if self._current_box is None or not self._tab.workdir:
            return
        path = self._tab.workdir / "grid.conf"
        path.write_text(self._current_box.to_vina_conf(), encoding="utf-8")
        self.lbl_save_status.setText(f"OK: Saved {path.name}")
        self._tab.append_log_info(f"Saved {path}")
        self._tab.maybe_show_citations()

    def _on_save_pml(self) -> None:
        if self._current_box is None or not self._tab.workdir:
            return
        path = self._tab.workdir / "grid_box.py"
        path.write_text(self._current_box.to_pymol_cgo(), encoding="utf-8")
        self.lbl_save_status.setText(f"OK: Saved {path.name}")
        self._tab.append_log_info(f"Saved {path}")
        # Tell the sidebar PyMOL panel about the new grid_box.py so the
        # main "Open in PyMOL" button now loads receptor + pockets + grid.
        self._tab.viewer.set_grid_box_py(path)

    def _on_open_pymol(self) -> None:
        wd = self._tab.workdir
        receptor = self._tab.receptor_pdbqt
        if not wd or receptor is None or not receptor.is_file():
            QMessageBox.warning(self, "Receptor required",
                                "Run Step 2 first so the receptor PDBQT is available.")
            return
        pymol = find_pymol()
        if not pymol:
            QMessageBox.warning(
                self, "PyMOL not found",
                "PyMOL is not on $PATH. Install it for external 3D visualisation.")
            return

        # Always rewrite grid_box.py from the current in-memory box so the
        # padding slider's latest value is reflected. Previously this only
        # wrote when the file didn't exist, which left a stale padding=10
        # script around after the first save.
        grid_pml = wd / "grid_box.py"
        if self._current_box is not None:
            grid_pml.write_text(self._current_box.to_pymol_cgo(), encoding="utf-8")

        # Locate the Fpocket .pml so the pockets show up too.
        fpocket_pml: Path | None = None
        out_dir = wd / (receptor.stem + "_out")
        if out_dir.is_dir():
            cand = out_dir / f"{receptor.stem}.pml"
            if cand.is_file():
                fpocket_pml = cand

        ok, msg = open_receptor_with_pockets_and_grid(
            pymol=pymol,
            receptor=receptor,
            fpocket_pml=fpocket_pml,
            grid_box_py=grid_pml if grid_pml.is_file() else None,
            workdir=wd,
        )
        if ok:
            self.lbl_save_status.setText(f"OK: PyMOL launched")
            self._tab.append_log_info(
                f"Launched PyMOL with {receptor.name}"
                + (f" + {fpocket_pml.name}" if fpocket_pml else "")
                + (f" + grid_box.py" if grid_pml.is_file() else "")
            )
        else:
            self._tab.append_log_info(f"PyMOL launch FAILED: {msg}")
            QMessageBox.warning(self, "PyMOL launch failed", msg)


# ===========================================================================
# ReceptorTab - orchestrator
# ===========================================================================
class ReceptorTab(BaseTab):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(settings, parent)
        self.cleaned_pdb: Path | None = None
        self.chains_kept: list[str] = []
        self.kept_waters: list = []          # filled by Step 1's _on_clean
        self.receptor_pdbqt: Path | None = None
        self._pocket_bundles: list[dict] = []
        self._build()

    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        outer = QSplitter(Qt.Horizontal)
        root.addWidget(outer)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(12, 12, 12, 12)

        self.step_list = QListWidget()
        self.step_list.setMaximumHeight(120)
        for label in STEPS:
            QListWidgetItem(label, self.step_list)
        self.step_list.setCurrentRow(0)
        self.step_list.currentRowChanged.connect(self._on_step_changed)
        lv.addWidget(self.step_list)

        self.step_stack = QStackedWidget()
        self.step1 = Step1Widget(self)
        self.step2 = Step2Widget(self)
        self.step3 = Step3Widget(self)
        self.step4 = Step4Widget(self)
        for w in (self.step1, self.step2, self.step3, self.step4):
            self.step_stack.addWidget(w)
        lv.addWidget(self.step_stack, 1)

        nav_row = QHBoxLayout()
        self.btn_prev = QPushButton("Previous")
        self.btn_next = QPushButton("Next")
        self.btn_prev.clicked.connect(lambda: self._move_step(-1))
        self.btn_next.clicked.connect(lambda: self._move_step(+1))
        nav_row.addWidget(self.btn_prev)
        nav_row.addWidget(self.btn_next)
        nav_row.addStretch(1)
        lv.addLayout(nav_row)
        outer.addWidget(left)

        right = QSplitter(Qt.Vertical)
        self.viewer = PymolPanel(self)
        right.addWidget(self.viewer)
        self.log = LogConsole(max_lines=2000)
        self.log.setMaximumHeight(180)
        right.addWidget(self.log)
        right.setStretchFactor(0, 4)
        right.setStretchFactor(1, 1)
        outer.addWidget(right)

        outer.setStretchFactor(0, 1)
        outer.setStretchFactor(1, 1)
        outer.setSizes([520, 760])

    def on_step1_done(self, cleaned_pdb: Path, chains: list[str]) -> None:
        self.cleaned_pdb = cleaned_pdb
        self.chains_kept = list(chains)
        self.receptor_pdbqt = None
        self._pocket_bundles = []
        self.step2.enable_run(True)
        self.step3.enable_run(False)
        self.step4.recompute()

    def on_step2_done(self, receptor_pdbqt: Path) -> None:
        self.receptor_pdbqt = receptor_pdbqt
        self._pocket_bundles = []
        self.step3.enable_run(True)
        self.step4.recompute()

    def set_pocket_bundles(self, bundles: list[dict]) -> None:
        self._pocket_bundles = list(bundles)
        self.step4.recompute()

    def on_pocket_toggled(self) -> None:
        self.step4.recompute()

    def selected_pocket_atoms(self) -> list[tuple[float, float, float]]:
        chosen = set(self.step3.selected_pocket_numbers())
        out: list[tuple[float, float, float]] = []
        for b in self._pocket_bundles:
            if b["number"] in chosen:
                out.extend(tuple(a) for a in b["atoms"])
        return out

    def append_log_info(self, msg: str) -> None:
        self.log.append_info(msg)

    def maybe_show_citations(self) -> None:
        CitationDialog.maybe_show(
            self, self.settings,
            title="jamreceptor - citations",
            body_html=JAMRECEPTOR_CITATIONS_HTML,
        )

    @Slot(int)
    def _on_step_changed(self, row: int) -> None:
        if 0 <= row < self.step_stack.count():
            self.step_stack.setCurrentIndex(row)

    def _move_step(self, delta: int) -> None:
        new = max(0, min(len(STEPS) - 1, self.step_list.currentRow() + delta))
        self.step_list.setCurrentRow(new)


def _wrap(layout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w
