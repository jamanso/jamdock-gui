"""Structural-water selection panel.

Embedded in Step 1 of the Receptor tab. Detects waters in the source PDB,
scores them against tunable criteria, and lets the user mix three modes:

* **None** — strip every water (default, identical to legacy jamreceptor).
* **Auto** — keep waters that pass the criteria (B, occupancy, polar
  neighbours, distance to ligand/grid).
* **Manual** — fine-grained per-water toggling in a table.

Exposes :meth:`kept_waters` so Step 1's ``_on_clean`` can re-inject them
into the cleaned PDB, and a :attr:`selection_changed` signal so the rest
of the UI (status labels, log) can react in real time.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jamdock_gui.core.waters import (
    Water,
    WaterCriteria,
    find_waters,
    score_waters,
)

if TYPE_CHECKING:
    from jamdock_gui.core.waters import Water  # noqa: F401


MODE_NONE = "none"
MODE_AUTO = "auto"
MODE_MANUAL = "manual"


class WaterPanel(QGroupBox):
    """UI for selecting structural waters to preserve through the pipeline."""

    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Structural waters (optional)", parent)
        self._waters: list[Water] = []
        self._pdb_path: Path | None = None
        self._chains_kept: set[str] | None = None
        self._fallback_anchor: tuple[float, float, float] | None = None
        # Manual mode: explicit per-water keep/skip overrides; key = water.label.
        self._manual_keep: dict[str, bool] = {}
        self._mode: str = MODE_NONE
        self._build()
        self._refresh_status()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build(self) -> None:
        v = QVBoxLayout(self)

        # ---- Status banner -------------------------------------------
        self.lbl_status = QLabel("No PDB loaded.")
        self.lbl_status.setWordWrap(True)
        v.addWidget(self.lbl_status)

        # ---- Mode selector -------------------------------------------
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("<b>Mode:</b>"))
        self.rb_none = QRadioButton("None (remove all)")
        self.rb_auto = QRadioButton("Auto (criteria below)")
        self.rb_manual = QRadioButton("Manual (table)")
        self.rb_none.setChecked(True)
        self._mode_group = QButtonGroup(self)
        for i, rb in enumerate((self.rb_none, self.rb_auto, self.rb_manual)):
            self._mode_group.addButton(rb, i)
            mode_row.addWidget(rb)
        mode_row.addStretch(1)
        v.addLayout(mode_row)
        self.rb_none.toggled.connect(lambda c: c and self._set_mode(MODE_NONE))
        self.rb_auto.toggled.connect(lambda c: c and self._set_mode(MODE_AUTO))
        self.rb_manual.toggled.connect(lambda c: c and self._set_mode(MODE_MANUAL))

        # ---- Criteria form (visible only in Auto mode) ---------------
        self._criteria_box = QGroupBox("Auto criteria")
        cform = QFormLayout(self._criteria_box)
        defaults = WaterCriteria()

        self.sp_b = QDoubleSpinBox()
        self.sp_b.setRange(0.0, 200.0); self.sp_b.setDecimals(1)
        self.sp_b.setSuffix(" Å²"); self.sp_b.setValue(defaults.max_b_factor)
        cform.addRow("Max B-factor:", self.sp_b)

        self.sp_occ = QDoubleSpinBox()
        self.sp_occ.setRange(0.0, 1.0); self.sp_occ.setDecimals(2)
        self.sp_occ.setSingleStep(0.05); self.sp_occ.setValue(defaults.min_occupancy)
        cform.addRow("Min occupancy:", self.sp_occ)

        self.sp_nbrs = QSpinBox()
        self.sp_nbrs.setRange(0, 10); self.sp_nbrs.setValue(defaults.min_polar_neighbors)
        cform.addRow("Min polar neighbours (within 3.5 Å):", self.sp_nbrs)

        self.sp_dist = QDoubleSpinBox()
        self.sp_dist.setRange(0.0, 30.0); self.sp_dist.setDecimals(1)
        self.sp_dist.setSuffix(" Å"); self.sp_dist.setValue(defaults.max_distance_to_ligand)
        cform.addRow("Max distance to ligand/grid:", self.sp_dist)

        self.sp_score = QSpinBox()
        self.sp_score.setRange(1, 4); self.sp_score.setValue(defaults.min_score_to_keep)
        cform.addRow("Min criteria met to keep:", self.sp_score)

        for w in (self.sp_b, self.sp_occ, self.sp_nbrs, self.sp_dist, self.sp_score):
            w.valueChanged.connect(self._on_criteria_changed)
        v.addWidget(self._criteria_box)

        # ---- Waters table --------------------------------------------
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Keep", "#", "Chain", "ResSeq", "B (Å²)", "Occ", "Nbrs", "Dist (Å)"]
        )
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(150)
        v.addWidget(self.table, 1)

        # Hint
        hint = QLabel(
            "<i>Tip: ★ marks waters that pass all criteria. In Manual mode, "
            "tick exactly the waters you want; in Auto mode the criteria above "
            "drive selection automatically.</i>"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("QLabel { color:#666; padding:4px; }")
        v.addWidget(hint)

        self._criteria_box.setEnabled(False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_pdb(
        self,
        pdb_path: Path | None,
        *,
        chains_kept: set[str] | None = None,
        fallback_anchor: tuple[float, float, float] | None = None,
    ) -> None:
        """Scan *pdb_path* for waters, score them, repopulate the table."""
        self._pdb_path = Path(pdb_path) if pdb_path else None
        self._chains_kept = chains_kept
        self._fallback_anchor = fallback_anchor
        self._manual_keep.clear()

        if self._pdb_path is None or not self._pdb_path.is_file():
            self._waters = []
            self._populate_table()
            self._refresh_status()
            return

        try:
            self._waters = find_waters(self._pdb_path)
        except (FileNotFoundError, OSError):
            self._waters = []
            self._populate_table()
            self._refresh_status()
            return

        self._rescore()
        self._populate_table()
        self._refresh_status()
        self.selection_changed.emit()

    def set_fallback_anchor(self, anchor: tuple[float, float, float] | None) -> None:
        """Update the anchor (e.g. grid-box centre) and re-score."""
        self._fallback_anchor = anchor
        if self._waters and self._pdb_path:
            self._rescore()
            self._populate_table()
            self.selection_changed.emit()

    def kept_waters(self) -> list[Water]:
        """Return the list of waters the user has chosen to preserve."""
        if self._mode == MODE_NONE or not self._waters:
            return []
        if self._mode == MODE_AUTO:
            return [w for w in self._waters if w.is_structural]
        # Manual
        return [w for w in self._waters
                if self._manual_keep.get(w.label, False)]

    def mode(self) -> str:
        return self._mode

    @property
    def has_waters(self) -> bool:
        return bool(self._waters)

    # ------------------------------------------------------------------
    # Mode + criteria handlers
    # ------------------------------------------------------------------
    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._criteria_box.setEnabled(mode == MODE_AUTO)
        # In Manual mode we expose the per-row checkboxes (always enabled);
        # in Auto / None they reflect the criteria result and are read-only.
        for row in range(self.table.rowCount()):
            cb = self._row_checkbox(row)
            if cb is None:
                continue
            cb.setEnabled(mode == MODE_MANUAL)
            cb.blockSignals(True)
            if mode == MODE_NONE:
                cb.setChecked(False)
            elif mode == MODE_AUTO:
                w = self._waters[row]
                cb.setChecked(w.is_structural)
            else:  # MANUAL
                cb.setChecked(self._manual_keep.get(self._waters[row].label, False))
            cb.blockSignals(False)
        self._refresh_status()
        self.selection_changed.emit()

    def _on_criteria_changed(self) -> None:
        if not self._waters:
            return
        self._rescore()
        self._populate_table(keep_manual=True)
        self._refresh_status()
        self.selection_changed.emit()

    def _build_criteria(self) -> WaterCriteria:
        return WaterCriteria(
            max_b_factor=self.sp_b.value(),
            min_occupancy=self.sp_occ.value(),
            min_polar_neighbors=self.sp_nbrs.value(),
            max_distance_to_ligand=self.sp_dist.value(),
            min_score_to_keep=self.sp_score.value(),
        )

    def _rescore(self) -> None:
        if not (self._pdb_path and self._waters):
            return
        score_waters(
            self._waters,
            self._pdb_path,
            chains_kept=self._chains_kept,
            fallback_anchor=self._fallback_anchor,
            criteria=self._build_criteria(),
        )

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------
    def _row_checkbox(self, row: int) -> QCheckBox | None:
        cell = self.table.cellWidget(row, 0)
        if cell is None:
            return None
        for child in cell.findChildren(QCheckBox):
            return child
        return None

    def _populate_table(self, *, keep_manual: bool = False) -> None:
        if not keep_manual:
            self._manual_keep.clear()
        self.table.setRowCount(len(self._waters))
        for row, w in enumerate(self._waters):
            cb = QCheckBox()
            if self._mode == MODE_AUTO:
                cb.setChecked(w.is_structural)
                cb.setEnabled(False)
            elif self._mode == MODE_MANUAL:
                cb.setChecked(self._manual_keep.get(w.label, w.is_structural))
                cb.setEnabled(True)
            else:
                cb.setChecked(False)
                cb.setEnabled(False)
            cb.stateChanged.connect(
                lambda _state, label=w.label, c=cb: self._on_row_toggled(label, c)
            )
            wrap = QWidget()
            hl = QHBoxLayout(wrap); hl.setContentsMargins(8, 0, 0, 0)
            hl.addWidget(cb); hl.addStretch(1)
            self.table.setCellWidget(row, 0, wrap)

            self.table.setItem(row, 1, _ti(str(row + 1), align_right=False))
            self.table.setItem(row, 2, _ti(w.chain))
            self.table.setItem(row, 3, _ti(f"{w.resseq}{w.icode.strip()}"))
            self.table.setItem(row, 4, _ti(f"{w.b_factor:.1f}"))
            self.table.setItem(row, 5, _ti(f"{w.occupancy:.2f}"))
            self.table.setItem(row, 6, _ti(str(w.n_polar_neighbors)))
            dist_str = "—" if w.dist_to_ligand is None else f"{w.dist_to_ligand:.1f}"
            self.table.setItem(row, 7, _ti(dist_str))

            # ★ mark the row as structural via background tint
            if w.is_structural:
                tint = QColor(220, 245, 225)
                for col in range(self.table.columnCount()):
                    item = self.table.item(row, col)
                    if item is not None:
                        item.setBackground(tint)

    def _on_row_toggled(self, label: str, cb: QCheckBox) -> None:
        if self._mode != MODE_MANUAL:
            return
        self._manual_keep[label] = cb.isChecked()
        self._refresh_status()
        self.selection_changed.emit()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def _refresh_status(self) -> None:
        if not self._waters:
            if self._pdb_path is None:
                self.lbl_status.setText("No PDB loaded.")
            else:
                self.lbl_status.setText("No waters detected in the source PDB.")
            return
        n_total = len(self._waters)
        n_struct = sum(1 for w in self._waters if w.is_structural)
        n_keep = len(self.kept_waters())
        anchor = (
            "ligand atoms found in the PDB"
            if any(w.dist_to_ligand is not None for w in self._waters)
            else "no ligand — anchor disabled"
        )
        self.lbl_status.setText(
            f"<b>{n_total}</b> waters detected · "
            f"<b>{n_struct}</b> ★ structural (current criteria) · "
            f"<b>{n_keep}</b> will be kept ({self._mode})  "
            f"<small style='color:#888'>[{anchor}]</small>"
        )


# ---------------------------------------------------------------------------
def _ti(text: str, *, align_right: bool = True) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if align_right:
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return item
