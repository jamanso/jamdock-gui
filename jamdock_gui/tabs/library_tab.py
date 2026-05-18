"""Tab 1 — Library Generation. Real implementation that wraps ``jamlib``.

Strategy
--------
We do **not** modify the original bash script. Instead we let jamlib's
existing interactive ``read`` prompts keep working and feed the answers via
``stdin``. This way the script stays 100% backward-compatible for CLI users
while the GUI gets a clean, non-interactive launch path.

The four custom-mode answers fed to stdin are::

    1                            ← option (1 = custom library)
    <mw_min> <mw_max>            ← MW range
    <logp_min> <logp_max>        ← LogP range
    <n_compounds>                ← total compounds

For FDA mode we feed a single line ``2`` and let the script run.

Live output (stdout/stderr) drives:
* the **progress bar** (parsed from jamlib's own ``[#### ] 23% (230/1000)``),
* a **phase label** that follows download → minimization → conversion → cleanup,
* the **log console** that mirrors what the user would see in a terminal.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from jamdock_gui.core.process_runner import RunnerConfig, ScriptRunner
from jamdock_gui.core.script_paths import ScriptNotFoundError, find_script
from jamdock_gui.deps import probe_obabel
from jamdock_gui.settings import LibraryDefaults
from jamdock_gui.tabs.base_tab import BaseTab
from jamdock_gui.widgets.citation_dialog import JAMLIB_CITATIONS_HTML, CitationDialog
from jamdock_gui.widgets.log_console import LogConsole


# Phase patterns — matched against each line of stdout in order.
JAMLIB_PHASE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Checking servers",          re.compile(r"Checking if FDA catalog", re.I)),
    ("Downloading compounds",     re.compile(r"Download in progress", re.I)),
    ("Energy minimization",       re.compile(r"energy minimization|Splitting compounds", re.I)),
    ("Selecting + converting",    re.compile(r"PDBQT conversion in progress", re.I)),
    ("Merging files",             re.compile(r"Merging complete", re.I)),
    ("Cleaning",                  re.compile(r"After cleaning|Conversion complete", re.I)),
    ("Library ready",             re.compile(r"ready to dock", re.I)),
]


class LibraryTab(BaseTab):
    def __init__(self, settings, parent=None) -> None:
        super().__init__(settings, parent)
        self._runner: ScriptRunner | None = None
        self._build()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        # -- mode selector -----------------------------------------------
        mode_box = QGroupBox("Library mode")
        mode_layout = QHBoxLayout(mode_box)
        self.rb_custom = QRadioButton("Custom library (MW / LogP / N)")
        self.rb_fda = QRadioButton("FDA-approved compounds (~3200)")
        self.rb_custom.setChecked(True)
        mode_layout.addWidget(self.rb_custom)
        mode_layout.addWidget(self.rb_fda)
        mode_layout.addStretch(1)
        root.addWidget(mode_box)

        # -- parameters stack --------------------------------------------
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_custom_panel())
        self.stack.addWidget(self._build_fda_panel())
        root.addWidget(self.stack)

        self.rb_custom.toggled.connect(
            lambda checked: self.stack.setCurrentIndex(0 if checked else 1)
        )

        # -- run controls -----------------------------------------------
        run_row = QHBoxLayout()
        self.btn_run = QPushButton("▶  Generate library")
        self.btn_stop = QPushButton("■  Stop")
        self.btn_stop.setEnabled(False)
        run_row.addWidget(self.btn_run)
        run_row.addWidget(self.btn_stop)
        run_row.addStretch(1)
        root.addLayout(run_row)

        # -- progress + phase + log -------------------------------------
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("Idle")
        root.addWidget(self.progress)

        self.phase_label = QLabel("Phase: idle")
        root.addWidget(self.phase_label)

        self.log = LogConsole()
        root.addWidget(self.log, 1)

        # -- wiring ------------------------------------------------------
        self.btn_run.clicked.connect(self._on_run_clicked)
        self.btn_stop.clicked.connect(self._on_stop_clicked)

    def _build_custom_panel(self) -> QWidget:
        d = self.settings.library_defaults()
        w = QWidget()
        form = QFormLayout(w)
        form.setLabelAlignment(Qt.AlignRight)

        self.sp_mw_min = QSpinBox()
        self.sp_mw_min.setRange(100, 9999)
        self.sp_mw_min.setValue(d.mw_min)
        self.sp_mw_max = QSpinBox()
        self.sp_mw_max.setRange(100, 9999)
        self.sp_mw_max.setValue(d.mw_max)
        mw_row = QHBoxLayout()
        mw_row.addWidget(self.sp_mw_min)
        mw_row.addWidget(QLabel("–"))
        mw_row.addWidget(self.sp_mw_max)
        mw_row.addWidget(QLabel("Da"))
        mw_row.addStretch(1)
        form.addRow("Molecular weight range:", _wrap(mw_row))

        self.sp_logp_min = QDoubleSpinBox()
        self.sp_logp_min.setDecimals(1)
        self.sp_logp_min.setRange(-5.0, 10.0)
        self.sp_logp_min.setValue(d.logp_min)
        self.sp_logp_max = QDoubleSpinBox()
        self.sp_logp_max.setDecimals(1)
        self.sp_logp_max.setRange(-5.0, 10.0)
        self.sp_logp_max.setValue(d.logp_max)
        logp_row = QHBoxLayout()
        logp_row.addWidget(self.sp_logp_min)
        logp_row.addWidget(QLabel("–"))
        logp_row.addWidget(self.sp_logp_max)
        logp_row.addStretch(1)
        form.addRow("LogP range:", _wrap(logp_row))

        self.sp_n = QSpinBox()
        self.sp_n.setRange(10, 1_000_000)
        self.sp_n.setSingleStep(100)
        self.sp_n.setValue(d.n_compounds)
        form.addRow("Number of compounds:", self.sp_n)

        return w

    def _build_fda_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        info = QLabel(
            "<p>This will download <b>~3200 FDA-approved compounds</b> from the "
            "ZINC FDA catalog (zinc.docking.org).</p>"
            "<p>Estimated time: <b>10–90 minutes</b> depending on server load.</p>"
            "<p>Output: <code>fda_pdbqt_compounds/</code> in the working directory.</p>"
        )
        info.setWordWrap(True)
        v.addWidget(info)
        v.addStretch(1)
        return w

    # ------------------------------------------------------------------
    # Run / Stop
    # ------------------------------------------------------------------
    def _on_run_clicked(self) -> None:
        # 1) Validate workdir
        if self.workdir is None:
            QMessageBox.warning(
                self,
                "Working directory required",
                "Please select a working directory first (top of the window).",
            )
            return

        if not os.access(self.workdir, os.W_OK):
            QMessageBox.warning(
                self,
                "Working directory not writable",
                f"The selected directory is not writable:\n{self.workdir}",
            )
            return

        # 2) Validate obabel (jamlib uses it for minimisation + PDBQT conversion).
        ob = probe_obabel(self.settings)
        if not ob.found:
            QMessageBox.warning(
                self,
                "Open Babel not found",
                "jamlib needs <b>obabel</b> (Open Babel). It was not found in "
                "$PATH.<br><br>"
                "Recommended install:<br>"
                "<code>conda install -c conda-forge openbabel</code>",
            )
            return

        # 3) Locate the bundled jamlib script (overridable via Settings).
        try:
            override = self.settings.get_script_override("jamlib")
            script = find_script("jamlib", override=override)
        except ScriptNotFoundError as exc:
            QMessageBox.critical(self, "jamlib not found", str(exc))
            return

        # 4) Build stdin payload from the selected mode.
        custom_mode = self.rb_custom.isChecked()
        if custom_mode:
            mw_min = self.sp_mw_min.value()
            mw_max = self.sp_mw_max.value()
            logp_min = self.sp_logp_min.value()
            logp_max = self.sp_logp_max.value()
            n = self.sp_n.value()

            if mw_min >= mw_max:
                QMessageBox.warning(self, "Invalid MW range",
                                    "MW max must be greater than MW min.")
                return
            if logp_min >= logp_max:
                QMessageBox.warning(self, "Invalid LogP range",
                                    "LogP max must be greater than LogP min.")
                return

            stdin_payload = (
                f"1\n"
                f"{mw_min} {mw_max}\n"
                f"{logp_min:g} {logp_max:g}\n"
                f"{n}\n"
            ).encode("utf-8")

            # Persist the chosen defaults so next launch remembers them.
            self.settings.set_library_defaults(LibraryDefaults(
                mw_min=mw_min, mw_max=mw_max,
                logp_min=logp_min, logp_max=logp_max,
                n_compounds=n,
            ))
            summary = (
                f"Custom library — MW {mw_min}–{mw_max}, "
                f"LogP {logp_min:g}–{logp_max:g}, N={n}"
            )
        else:
            stdin_payload = b"2\n"
            summary = "FDA-approved compounds (~3200)"

        # 5) Build and start the runner.
        cfg = RunnerConfig(
            script=script,
            workdir=self.workdir,
            stdin_input=stdin_payload,
            phase_patterns=JAMLIB_PHASE_PATTERNS,
        )
        self._runner = ScriptRunner(self)
        self._runner.line.connect(self.log.append_stdout)
        self._runner.err_line.connect(self.log.append_stderr)
        self._runner.phase_changed.connect(self._on_phase_changed)
        self._runner.progress.connect(self._on_progress)
        self._runner.started.connect(self._on_started)
        self._runner.finished.connect(self._on_finished)
        self._runner.failed_to_start.connect(self._on_failed_to_start)

        self.log.clear_log()
        self.log.append_info(f"▶ Starting jamlib — {summary}")
        self.log.append_info(f"   workdir: {self.workdir}")
        self.log.append_info(f"   script:  {script}")
        try:
            self._runner.start(cfg)
        except RuntimeError as exc:
            QMessageBox.critical(self, "Runner error", str(exc))

    def _on_stop_clicked(self) -> None:
        if self._runner and self._runner.is_running():
            self.log.append_info("■ Stop requested — sending SIGTERM (5s grace)…")
            self._runner.stop()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    @Slot()
    def _on_started(self) -> None:
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self.progress.setFormat("Starting…")
        self.phase_label.setText("Phase: starting")

    @Slot(str)
    def _on_phase_changed(self, label: str) -> None:
        self.phase_label.setText(f"Phase: {label}")
        self.log.append_info(f"  ◆ {label}")

    @Slot(int, int)
    def _on_progress(self, current: int, total: int) -> None:
        if total <= 0:
            self.progress.setRange(0, 0)  # indeterminate
            return
        pct = round(100 * current / total)
        self.progress.setRange(0, 100)
        self.progress.setValue(pct)
        self.progress.setFormat(f"{pct}%  ({current}/{total})")

    @Slot(int, int)
    def _on_finished(self, exit_code: int, exit_status: int) -> None:
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._runner = None

        if exit_code == 0:
            self.progress.setValue(100)
            self.progress.setFormat("Done")
            self.phase_label.setText("Phase: completed")
            self.log.append_info("✓ jamlib finished successfully.")
            CitationDialog.maybe_show(
                self,
                self.settings,
                title="jamlib — citations",
                body_html=JAMLIB_CITATIONS_HTML,
            )
        else:
            self.progress.setFormat(f"Exit {exit_code}")
            self.phase_label.setText("Phase: failed")
            self.log.append_info(f"✗ jamlib finished with exit code {exit_code}.")

    @Slot(str)
    def _on_failed_to_start(self, message: str) -> None:
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._runner = None
        self.log.append_stderr(f"Failed to start: {message}")
        QMessageBox.critical(self, "Failed to start", message)


def _wrap(layout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w
