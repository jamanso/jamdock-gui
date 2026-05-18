"""Tab 3 — Run Docking. Replaces ``jamqvina`` (and ``jamresume``).

Drives :class:`DockingPool` from the GUI: validates inputs, spins up the
parallel workers, surfaces live progress / throughput / ETA, and updates
the per-ligand table as each job finishes.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jamdock_gui.core.docking import (
    DockingConfig,
    DockingPool,
    discover_jobs,
    restore_state_into_jobs,
)
from jamdock_gui.core.state import load_state
from jamdock_gui.deps import probe_all
from jamdock_gui.settings import DockingDefaults, Settings
from jamdock_gui.tabs.base_tab import BaseTab
from jamdock_gui.widgets.citation_dialog import (
    JAMQVINA_CITATIONS_HTML,
    CitationDialog,
)
from jamdock_gui.widgets.log_console import LogConsole
from jamdock_gui.widgets.throughput_chart import ThroughputChart


_STATUS_COLORS: dict[str, QColor] = {
    "queued":  QColor("#999"),
    "running": QColor("#1f6feb"),
    "done":    QColor("#1e8449"),
    "failed":  QColor("#c0392b"),
    "skipped": QColor("#888"),
}

_STATUS_LABELS: dict[str, str] = {
    "queued":  "⌛ queued",
    "running": "▶ running",
    "done":    "✓ done",
    "failed":  "✗ failed",
    "skipped": "— skipped",
}


def _fmt_seconds(secs: int | None) -> str:
    if secs is None or secs < 0:
        return "—"
    secs = int(secs)
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class DockingTab(BaseTab):
    # Cross-tab signals consumed by the Results tab so it can show live updates.
    pool_started = Signal(object)   # emits the DockingPool
    pool_stopped = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(settings, parent)
        self._pool: DockingPool | None = None
        self._running: bool = False
        self._build()
        self.refresh_from_workdir()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        # -- inputs -------------------------------------------------------
        inputs_box = QGroupBox("Inputs (auto-detected from working directory)")
        f = QFormLayout(inputs_box)

        self.lbl_receptor = QLabel("— not detected —")
        self.lbl_receptor.setTextInteractionFlags(Qt.TextSelectableByMouse)
        btn_pick_receptor = QPushButton("Browse…")
        btn_pick_receptor.clicked.connect(self._pick_receptor)
        rrow = QHBoxLayout()
        rrow.addWidget(self.lbl_receptor, 1)
        rrow.addWidget(btn_pick_receptor)
        f.addRow("Receptor (.pdbqt):", _wrap(rrow))

        self.lbl_grid = QLabel("— not detected —")
        btn_pick_grid = QPushButton("Browse…")
        btn_pick_grid.clicked.connect(self._pick_grid)
        grow = QHBoxLayout()
        grow.addWidget(self.lbl_grid, 1)
        grow.addWidget(btn_pick_grid)
        f.addRow("Grid configuration:", _wrap(grow))

        self.lbl_library = QLabel("— not detected —")
        btn_pick_lib = QPushButton("Browse…")
        btn_pick_lib.clicked.connect(self._pick_library)
        lrow = QHBoxLayout()
        lrow.addWidget(self.lbl_library, 1)
        lrow.addWidget(btn_pick_lib)
        f.addRow("Ligand library:", _wrap(lrow))

        self.lbl_count = QLabel("0 ligands")
        f.addRow("Ligands found:", self.lbl_count)
        root.addWidget(inputs_box)

        # -- QuickVina parameters -----------------------------------------
        params_box = QGroupBox("QuickVina 2 parameters")
        pform = QFormLayout(params_box)

        d = self.settings.docking_defaults()
        self.sp_exh = QSpinBox(); self.sp_exh.setRange(1, 64); self.sp_exh.setValue(d.exhaustiveness)
        pform.addRow("Exhaustiveness:", self.sp_exh)

        self.sp_modes = QSpinBox(); self.sp_modes.setRange(1, 50); self.sp_modes.setValue(d.num_modes)
        pform.addRow("Num modes:", self.sp_modes)

        self.sp_energy = QDoubleSpinBox()
        self.sp_energy.setRange(0.1, 20.0); self.sp_energy.setSingleStep(0.5)
        self.sp_energy.setValue(d.energy_range)
        pform.addRow("Energy range (kcal/mol):", self.sp_energy)

        self.sp_cpu = QSpinBox(); self.sp_cpu.setRange(1, 256); self.sp_cpu.setValue(d.cpu_per_job)
        pform.addRow("CPUs per job:", self.sp_cpu)

        self.sp_parallel = QSpinBox(); self.sp_parallel.setRange(1, 64); self.sp_parallel.setValue(d.parallel_jobs)
        pform.addRow("Parallel jobs:", self.sp_parallel)
        root.addWidget(params_box)

        # -- run controls -------------------------------------------------
        ctrl_row = QHBoxLayout()
        self.btn_start = QPushButton("▶  Start / Resume")
        self.btn_pause = QPushButton("Pause")
        self.btn_stop = QPushButton("■  Stop")
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_pause.clicked.connect(self._on_pause_resume)
        self.btn_stop.clicked.connect(self._on_stop)
        ctrl_row.addWidget(self.btn_start)
        ctrl_row.addWidget(self.btn_pause)
        ctrl_row.addWidget(self.btn_stop)
        ctrl_row.addStretch(1)
        root.addLayout(ctrl_row)

        # Resume banner (hidden until start fills it).
        self.lbl_banner = QLabel("")
        self.lbl_banner.setStyleSheet(
            "QLabel { background: #fff8e1; border: 1px solid #ffe082;"
            " padding: 6px; border-radius: 4px; }"
        )
        self.lbl_banner.hide()
        root.addWidget(self.lbl_banner)

        # -- progress -----------------------------------------------------
        prog_box = QGroupBox("Progress")
        pv = QVBoxLayout(prog_box)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100); self.progress.setValue(0)
        self.progress.setFormat("Idle")
        pv.addWidget(self.progress)
        self.lbl_throughput = QLabel("Throughput: — · ETA: — · Done: 0/0")
        pv.addWidget(self.lbl_throughput)
        root.addWidget(prog_box)

        # -- table + chart split -----------------------------------------
        splitter = QSplitter(Qt.Horizontal)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Ligand", "Status", "Best score", "Modes", "Time (s)"]
        )
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 5):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        splitter.addWidget(self.table)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.chart = ThroughputChart()
        rv.addWidget(self.chart, 2)
        self.log = LogConsole(max_lines=2000)
        self.log.setMaximumHeight(180)
        rv.addWidget(self.log, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    # ------------------------------------------------------------------
    # Auto-detect inputs from workdir
    # ------------------------------------------------------------------
    def refresh_from_workdir(self) -> None:
        wd = self.workdir
        if not wd:
            self.lbl_receptor.setText("— set a working directory first —")
            self.lbl_grid.setText("— set a working directory first —")
            self.lbl_library.setText("— set a working directory first —")
            self.lbl_count.setText("0 ligands")
            return

        rec = sorted(wd.glob("*_for_docking.pdbqt"))
        self._receptor_path: Path | None = rec[-1] if rec else None
        self.lbl_receptor.setText(str(self._receptor_path) if self._receptor_path else "— not detected —")

        grid = wd / "grid.conf"
        self._grid_path: Path | None = grid if grid.is_file() else None
        self.lbl_grid.setText(str(self._grid_path) if self._grid_path else "— not detected —")

        lib_dir: Path | None = None
        if (wd / "fda_pdbqt_compounds").is_dir():
            lib_dir = wd / "fda_pdbqt_compounds"
        else:
            for d in sorted(wd.glob("library_pdbqt_*")):
                if d.is_dir():
                    lib_dir = d
                    break
        self._library_dir: Path | None = lib_dir
        if lib_dir:
            n = sum(1 for _ in lib_dir.glob("*.pdbqt"))
            self.lbl_library.setText(str(lib_dir))
            self.lbl_count.setText(f"{n} ligands")
        else:
            self.lbl_library.setText("— not detected —")
            self.lbl_count.setText("0 ligands")

        # Pre-populate the table with what's discoverable.
        self._refresh_table_from_disk()

        # If a previous .jamdock_state.json exists, hint at it.
        if load_state(wd) is not None:
            self.lbl_banner.setText(
                "ℹ  A previous docking run was detected in this folder. "
                "Click <b>Start / Resume</b> to continue from where it stopped."
            )
            self.lbl_banner.show()

    # ------------------------------------------------------------------
    # Pickers
    # ------------------------------------------------------------------
    def _pick_receptor(self) -> None:
        start = str(self.workdir or Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Pick receptor PDBQT", start,
            "PDBQT (*.pdbqt);;All files (*)"
        )
        if chosen:
            self._receptor_path = Path(chosen)
            self.lbl_receptor.setText(chosen)

    def _pick_grid(self) -> None:
        start = str(self.workdir or Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Pick grid.conf", start,
            "Config (*.conf *.txt);;All files (*)"
        )
        if chosen:
            self._grid_path = Path(chosen)
            self.lbl_grid.setText(chosen)

    def _pick_library(self) -> None:
        start = str(self.workdir or Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Pick ligand library directory", start)
        if chosen:
            self._library_dir = Path(chosen)
            n = sum(1 for _ in Path(chosen).glob("*.pdbqt"))
            self.lbl_library.setText(chosen)
            self.lbl_count.setText(f"{n} ligands")

    # ------------------------------------------------------------------
    # Run / Pause / Stop
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        wd = self.workdir
        if not wd:
            QMessageBox.warning(self, "Working directory required",
                                "Pick a working directory first.")
            return
        if not getattr(self, "_receptor_path", None):
            QMessageBox.warning(self, "Receptor required",
                                "Run Tab 2 (Receptor) or pick a PDBQT manually.")
            return
        if not getattr(self, "_grid_path", None):
            QMessageBox.warning(self, "Grid configuration required",
                                "Need a ``grid.conf`` (Tab 2 generates one).")
            return
        if not getattr(self, "_library_dir", None):
            QMessageBox.warning(self, "Ligand library required",
                                "Pick a folder with ``*.pdbqt`` ligands.")
            return

        statuses = probe_all(self.settings)
        qvina_st = statuses.get("qvina02")
        if not qvina_st or not qvina_st.found:
            QMessageBox.warning(
                self, "qvina02 not found",
                "QuickVina 2 (qvina02) is not on $PATH. Install it and "
                "ensure it's executable.")
            return

        cpu_total = (self.sp_cpu.value() * self.sp_parallel.value())
        import os as _os
        avail = _os.cpu_count() or 1
        if cpu_total > avail:
            ret = QMessageBox.question(
                self, "CPU oversubscription",
                f"You requested {self.sp_parallel.value()} jobs × "
                f"{self.sp_cpu.value()} CPUs = {cpu_total}, but only "
                f"{avail} are available. Continue anyway?"
            )
            if ret != QMessageBox.Yes:
                return

        # Persist defaults so the next launch remembers.
        self.settings.set_docking_defaults(DockingDefaults(
            exhaustiveness=self.sp_exh.value(),
            num_modes=self.sp_modes.value(),
            energy_range=self.sp_energy.value(),
            cpu_per_job=self.sp_cpu.value(),
            parallel_jobs=self.sp_parallel.value(),
        ))

        output_dir = wd / "docking_results"
        cfg = DockingConfig(
            qvina_path=Path(qvina_st.path or "qvina02"),
            receptor=self._receptor_path,
            grid_conf=self._grid_path,
            output_dir=output_dir,
            workdir=wd,
            exhaustiveness=self.sp_exh.value(),
            num_modes=self.sp_modes.value(),
            energy_range=self.sp_energy.value(),
            cpu_per_job=self.sp_cpu.value(),
            parallel_jobs=self.sp_parallel.value(),
        )

        jobs = discover_jobs(self._library_dir, output_dir)
        if not jobs:
            QMessageBox.warning(self, "No ligands",
                                f"No ``*.pdbqt`` files found in {self._library_dir}.")
            return
        # Carry over scores from a previous .jamdock_state.json if any.
        restore_state_into_jobs(load_state(wd), jobs)

        # Reset UI ----------------------------------------------------
        self._populate_table(jobs)
        self.chart.reset()
        self.progress.setRange(0, len(jobs))
        self.progress.setValue(0)
        self.progress.setFormat(f"0 / {len(jobs)}")
        self.log.clear_log()
        self.log.append_info(
            f"▶ Starting docking — {len(jobs)} ligands, "
            f"{cfg.parallel_jobs} jobs × {cfg.cpu_per_job} CPUs"
        )
        self.lbl_banner.hide()

        # Build the pool
        pool = DockingPool(self)
        pool.resume_summary.connect(self._on_resume_summary)
        pool.job_started.connect(self._on_job_started)
        pool.job_finished.connect(self._on_job_finished)
        pool.job_failed.connect(self._on_job_failed)
        pool.log_line.connect(self.log.append_stdout)
        pool.err_line.connect(self.log.append_stderr)
        pool.progress.connect(self._on_progress)
        pool.throughput.connect(self._on_throughput)
        pool.eta_changed.connect(self._on_eta)
        pool.all_finished.connect(self._on_all_finished)
        pool.paused_changed.connect(self._on_paused_changed)

        self._pool = pool
        self._running = True
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("Pause")
        self.btn_stop.setEnabled(True)

        try:
            pool.start(jobs, cfg)
        except RuntimeError as exc:
            QMessageBox.critical(self, "Failed to start", str(exc))
            self._teardown_run()
            return

        # Notify Tab 4 so it can subscribe to live updates.
        self.pool_started.emit(pool)

    def _on_pause_resume(self) -> None:
        if not self._pool:
            return
        if self.btn_pause.text().startswith("⏸"):
            self._pool.pause()
        else:
            self._pool.resume()

    def _on_stop(self) -> None:
        if not self._pool:
            return
        ret = QMessageBox.question(
            self, "Stop docking",
            "Cancel in-flight jobs? Already-finished ones are kept; "
            "you can resume later from the same working directory."
        )
        if ret == QMessageBox.Yes:
            self.log.append_info("■ Stopping — killing in-flight workers…")
            self._pool.stop()

    # ------------------------------------------------------------------
    # Slots from DockingPool
    # ------------------------------------------------------------------
    @Slot(int, int)
    def _on_resume_summary(self, n_done: int, n_todo: int) -> None:
        if n_done == 0:
            return
        self.lbl_banner.setText(
            f"ℹ  Resume detected — {n_done} ligands already done, "
            f"{n_todo} remaining."
        )
        self.lbl_banner.show()
        self.log.append_info(f"  Resuming: {n_done} done, {n_todo} to go.")

    @Slot(int)
    def _on_job_started(self, idx: int) -> None:
        if not self._pool:
            return
        job = self._pool.jobs[idx]
        row = self._row_for(idx, job)
        self._set_row_status(row, "running")

    @Slot(int)
    def _on_job_finished(self, idx: int) -> None:
        if not self._pool:
            return
        job = self._pool.jobs[idx]
        row = self._row_for(idx, job)
        self._set_row_status(row, "done")
        self._set_row_value(row, 2, f"{job.best_score:.2f}" if job.best_score is not None else "—")
        self._set_row_value(row, 3, str(job.n_modes))
        if job.duration_s is not None:
            self._set_row_value(row, 4, f"{job.duration_s:.1f}")

    @Slot(int, str)
    def _on_job_failed(self, idx: int, msg: str) -> None:
        if not self._pool:
            return
        job = self._pool.jobs[idx]
        row = self._row_for(idx, job)
        self._set_row_status(row, "failed")
        if job.duration_s is not None:
            self._set_row_value(row, 4, f"{job.duration_s:.1f}")
        self.log.append_stderr(f"{job.name}: {msg}")

    @Slot(int, int)
    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress.setFormat(f"{done} / {total}")
        self._update_throughput_label(done=done, total=total)

    @Slot(float)
    def _on_throughput(self, rate_per_min: float) -> None:
        done = self._pool.done if self._pool else 0
        self.chart.add_sample(rate_per_min, done)
        self._last_rate = rate_per_min
        self._update_throughput_label()

    @Slot(int)
    def _on_eta(self, eta_s: int) -> None:
        self._last_eta = eta_s
        self._update_throughput_label()

    @Slot()
    def _on_all_finished(self) -> None:
        self.log.append_info("✓ All ligands processed.")
        self._teardown_run()
        if self._pool and any(j.status == "done" for j in self._pool.jobs):
            CitationDialog.maybe_show(
                self, self.settings,
                title="jamqvina — citations",
                body_html=JAMQVINA_CITATIONS_HTML,
            )

    @Slot(bool)
    def _on_paused_changed(self, paused: bool) -> None:
        self.btn_pause.setText("Resume" if paused else "Pause")
        self.log.append_info("⏸  Paused — finishing in-flight jobs only." if paused
                             else "▶  Resumed.")

    # ------------------------------------------------------------------
    # Table helpers
    # ------------------------------------------------------------------
    def _populate_table(self, jobs) -> None:
        """Lazy-fill: only render rows for jobs that aren't queued.

        For very large libraries (e.g. 500k ligands) populating every row
        up front would freeze the UI for tens of minutes — QTableWidget
        does O(N) work per cell. Instead we keep the table empty at start
        and let :meth:`_on_job_started` / ``_on_job_finished`` append a
        row only when a job actually becomes interesting.

        Already-finished jobs (resume case) are pre-added so the user sees
        them on relaunch.
        """
        # Map *original job index* → table row index. Filled as rows appear.
        self._row_for_job: dict[int, int] = {}
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(0)
            for idx, job in enumerate(jobs):
                if job.status not in ("done", "failed", "running"):
                    continue       # queued / skipped → don't render yet
                self._append_row(idx, job)
        finally:
            self.table.setUpdatesEnabled(True)
            self.table.setSortingEnabled(True)

    def _append_row(self, job_idx: int, job) -> int:
        """Append a row for *job* (by original index) and return the row idx."""
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._row_for_job[job_idx] = row
        self.table.setItem(row, 0, QTableWidgetItem(job.name))
        initial = job.status if job.status in _STATUS_LABELS else "queued"
        self._set_row_status(row, initial)
        self._set_row_value(row, 2,
            f"{job.best_score:.2f}" if job.best_score is not None else "—")
        self._set_row_value(row, 3, str(job.n_modes) if job.n_modes else "—")
        self._set_row_value(row, 4,
            f"{job.duration_s:.1f}" if job.duration_s is not None else "—")
        return row

    def _row_for(self, job_idx: int, job) -> int:
        """Return the table row for *job_idx*, creating it lazily if needed."""
        row = self._row_for_job.get(job_idx)
        if row is None:
            row = self._append_row(job_idx, job)
        return row

    def _refresh_table_from_disk(self) -> None:
        wd = self.workdir
        if not wd or not getattr(self, "_library_dir", None):
            self.table.setRowCount(0)
            return
        out_dir = wd / "docking_results"
        jobs = discover_jobs(self._library_dir, out_dir)
        restore_state_into_jobs(load_state(wd), jobs)
        # Update status from disk presence (resume preview).
        for j in jobs:
            try:
                if j.out_pdbqt.is_file() and j.out_pdbqt.stat().st_size > 0:
                    j.status = "done"
            except OSError:
                pass
        self._populate_table(jobs)

    def _set_row_status(self, row: int, status: str) -> None:
        item = QTableWidgetItem(_STATUS_LABELS.get(status, status))
        color = _STATUS_COLORS.get(status)
        if color:
            item.setForeground(color)
        self.table.setItem(row, 1, item)
        if status == "running":
            self.table.scrollToItem(item)

    def _set_row_value(self, row: int, col: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setItem(row, col, item)

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def _update_throughput_label(self, *, done: int | None = None, total: int | None = None) -> None:
        if done is None or total is None:
            done = self._pool.done if self._pool else 0
            total = self._pool.total if self._pool else 0
        rate = getattr(self, "_last_rate", None)
        eta = getattr(self, "_last_eta", None)
        rate_str = f"{rate:.1f} comp/min" if rate else "—"
        eta_str = _fmt_seconds(eta)
        self.lbl_throughput.setText(
            f"Throughput: {rate_str}  ·  ETA: {eta_str}  ·  Done: {done}/{total}"
        )

    def _teardown_run(self) -> None:
        self._running = False
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("Pause")
        self.btn_stop.setEnabled(False)
        # Tell Tab 4 the live feed is done.
        self.pool_stopped.emit()


# ---------------------------------------------------------------------------
def _wrap(layout) -> QWidget:
    w = QWidget(); w.setLayout(layout); return w
