"""Qt model + proxy for the Results table.

Design
------
- :class:`ResultsTableModel` is a thin :class:`QAbstractTableModel` over a
  list of :class:`ResultRow`. Sorting is delegated to the proxy.
- :class:`ResultsFilterProxy` extends :class:`QSortFilterProxyModel` with
  the user-facing filters (max affinity, min sim score, MW range, etc.)
  plus an optional ``top_n`` post-filter that crops to the best N rows
  AFTER the column sort is applied. That gives "top 50 hits" behaviour
  identical to ``jamrank``.

Why two layers
--------------
The base model is the source of truth — Tab 4 inserts rows into it as
docking finishes. The proxy is purely view-side: filters and sort change
without touching the underlying data, so live insertions don't disturb
the user's current view.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt,
)
from PySide6.QtGui import QBrush, QColor

from jamdock_gui.core.results import ResultRow, ro5_violation_details


# ---------------------------------------------------------------------------
# Column layout
# ---------------------------------------------------------------------------
COL_RANK     = 0
COL_LIGAND   = 1
COL_AFFINITY = 2
COL_SIMSCORE = 3
COL_MODES    = 4
COL_MW       = 5
COL_LOGP     = 6
COL_HBD      = 7
COL_HBA      = 8
COL_RO5      = 9
COL_ZINC     = 10
COL_NOTE     = 11

COLUMNS: tuple[tuple[int, str, str], ...] = (
    (COL_RANK,     "Rank",       "Position in current sort order"),
    (COL_LIGAND,   "Ligand",     "Filename stem (matches the .pdbqt)"),
    (COL_AFFINITY, "Affinity",   "Best-mode docking energy (kcal/mol)"),
    (COL_SIMSCORE, "SimScore",   "Pose convergence (0..100); higher is more reliable"),
    (COL_MODES,    "Modes",      "Number of poses returned"),
    (COL_MW,       "MW (Da)",    "Molecular weight (RDKit, from input SDF)"),
    (COL_LOGP,     "LogP",       "Crippen LogP (RDKit) — Lipinski rule: ≤ 5"),
    (COL_HBD,      "HBD",        "H-bond donors — Lipinski rule: ≤ 5"),
    (COL_HBA,      "HBA",        "H-bond acceptors — Lipinski rule: ≤ 10"),
    (COL_RO5,      "Drug-like?", "Lipinski's Rule of Five — ★ if 0 violations"),
    (COL_ZINC,     "ZINC ID",    "Identifier in the ZINC database (linkable)"),
    (COL_NOTE,     "Note",       "Errors or extra info"),
)


# Soft green for Ro5-passing rows. Light enough not to blind, dark enough
# to read black text on it.
_RO5_PASS_BG = QColor(220, 245, 225)


# ---------------------------------------------------------------------------
# Base model
# ---------------------------------------------------------------------------
class ResultsTableModel(QAbstractTableModel):
    """Holds a list of :class:`ResultRow` and exposes them to a :class:`QTableView`."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[ResultRow] = []
        self._index_by_ligand: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(COLUMNS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole
    ) -> Any:
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return COLUMNS[section][1]
        if role == Qt.ToolTipRole:
            return COLUMNS[section][2]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            if col == COL_RANK:
                return index.row() + 1   # 1-based; proxy will overwrite
            if col == COL_LIGAND:
                return row.ligand
            if col == COL_AFFINITY:
                return f"{row.affinity:.2f}" if row.affinity is not None else "—"
            if col == COL_SIMSCORE:
                return f"{row.sim_score} %" if row.sim_score is not None else "—"
            if col == COL_MODES:
                return row.n_modes if row.n_modes else "—"
            if col == COL_MW:
                return f"{row.mw:.1f}" if row.mw is not None else "—"
            if col == COL_LOGP:
                return f"{row.logp:.2f}" if row.logp is not None else "—"
            if col == COL_HBD:
                return row.hbd if row.hbd is not None else "—"
            if col == COL_HBA:
                return row.hba if row.hba is not None else "—"
            if col == COL_RO5:
                if row.ro5_pass is None:
                    return "—"
                return "★" if row.ro5_pass else f"{row.ro5_violations} viol."
            if col == COL_ZINC:
                return row.zinc_id or "—"
            if col == COL_NOTE:
                return row.error or ""
            return None

        if role == Qt.UserRole:
            # Sort key — return the raw value so the proxy can sort numerically.
            if col == COL_AFFINITY:
                # Non-existent affinities go to the end of an ascending sort.
                return float("inf") if row.affinity is None else row.affinity
            if col == COL_SIMSCORE:
                return -1 if row.sim_score is None else row.sim_score
            if col == COL_MODES:
                return row.n_modes
            if col == COL_MW:
                return float("inf") if row.mw is None else row.mw
            if col == COL_LOGP:
                return float("inf") if row.logp is None else row.logp
            if col == COL_HBD:
                return -1 if row.hbd is None else row.hbd
            if col == COL_HBA:
                return -1 if row.hba is None else row.hba
            if col == COL_RO5:
                # Sort by violations ascending (fewest first → ★ on top).
                return 99 if row.ro5_violations is None else row.ro5_violations
            if col == COL_ZINC:
                return row.zinc_id or ""
            if col == COL_LIGAND:
                # Numerical ordering when the stem is a pure number (jamlib output).
                try:
                    return int(row.ligand)
                except ValueError:
                    return row.ligand
            return None

        if role == Qt.TextAlignmentRole:
            if col in (COL_AFFINITY, COL_SIMSCORE, COL_MODES, COL_MW,
                       COL_LOGP, COL_HBD, COL_HBA, COL_RO5, COL_RANK):
                return int(Qt.AlignRight | Qt.AlignVCenter)

        if role == Qt.ToolTipRole:
            if col == COL_ZINC and row.zinc_link:
                return row.zinc_link
            if col == COL_RO5:
                if row.ro5_pass is None:
                    return "Lipinski metrics not available (no SDF or no RDKit)"
                if row.ro5_pass:
                    return ("Passes Lipinski's Rule of Five "
                            "(MW≤500, LogP≤5, HBD≤5, HBA≤10)")
                viols = ro5_violation_details(row)
                return "Violations: " + "; ".join(viols) if viols else "Has violations"

        if role == Qt.BackgroundRole and row.ro5_pass is True:
            # Tint the whole row light green when the compound is "drug-like".
            return QBrush(_RO5_PASS_BG)

        if role == Qt.ForegroundRole and row.error:
            return QBrush(QColor("#c0392b"))

        return None

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def replace_all(self, rows: list[ResultRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._index_by_ligand = {r.ligand: i for i, r in enumerate(self._rows)}
        self.endResetModel()

    def upsert(self, row: ResultRow) -> None:
        existing = self._index_by_ligand.get(row.ligand)
        if existing is None:
            new_idx = len(self._rows)
            self.beginInsertRows(QModelIndex(), new_idx, new_idx)
            self._rows.append(row)
            self._index_by_ligand[row.ligand] = new_idx
            self.endInsertRows()
        else:
            self._rows[existing] = row
            top_left = self.index(existing, 0)
            bot_right = self.index(existing, self.columnCount() - 1)
            self.dataChanged.emit(top_left, bot_right)

    def row_at(self, source_row: int) -> ResultRow | None:
        if 0 <= source_row < len(self._rows):
            return self._rows[source_row]
        return None

    def all_rows(self) -> list[ResultRow]:
        return list(self._rows)


# ---------------------------------------------------------------------------
# Filter proxy
# ---------------------------------------------------------------------------
class ResultsFilterProxy(QSortFilterProxyModel):
    """Filter + top-N proxy. Sorts via :data:`Qt.UserRole` (raw values)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSortRole(Qt.UserRole)
        self._max_affinity: float | None = None
        self._min_sim_score: int | None = None
        self._min_modes: int | None = None
        self._mw_min: float | None = None
        self._mw_max: float | None = None
        self._only_with_zinc: bool = False
        self._only_ro5: bool = False
        self._top_n: int | None = None

    # ------------------------------------------------------------------
    # Filter setters (each invalidates only the filter, keeps the sort)
    # ------------------------------------------------------------------
    def set_max_affinity(self, value: float | None) -> None:
        self._max_affinity = value
        self.invalidateFilter()

    def set_min_sim_score(self, value: int | None) -> None:
        self._min_sim_score = value
        self.invalidateFilter()

    def set_min_modes(self, value: int | None) -> None:
        self._min_modes = value
        self.invalidateFilter()

    def set_mw_range(self, mw_min: float | None, mw_max: float | None) -> None:
        self._mw_min, self._mw_max = mw_min, mw_max
        self.invalidateFilter()

    def set_only_with_zinc(self, value: bool) -> None:
        self._only_with_zinc = bool(value)
        self.invalidateFilter()

    def set_only_ro5(self, value: bool) -> None:
        self._only_ro5 = bool(value)
        self.invalidateFilter()

    def set_top_n(self, value: int | None) -> None:
        self._top_n = value
        self.invalidate()  # top-N depends on sort, not just filter

    def reset_filters(self) -> None:
        self._max_affinity = None
        self._min_sim_score = None
        self._min_modes = None
        self._mw_min = self._mw_max = None
        self._only_with_zinc = False
        self._only_ro5 = False
        self._top_n = None
        self.invalidate()

    # ------------------------------------------------------------------
    # Filter logic
    # ------------------------------------------------------------------
    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if not isinstance(model, ResultsTableModel):
            return True
        row = model.row_at(source_row)
        if row is None:
            return False

        if self._max_affinity is not None:
            if row.affinity is None or row.affinity > self._max_affinity:
                return False
        if self._min_sim_score is not None:
            if row.sim_score is None or row.sim_score < self._min_sim_score:
                return False
        if self._min_modes is not None:
            if row.n_modes < self._min_modes:
                return False
        if self._mw_min is not None or self._mw_max is not None:
            if row.mw is None:
                return False
            if self._mw_min is not None and row.mw < self._mw_min:
                return False
            if self._mw_max is not None and row.mw > self._mw_max:
                return False
        if self._only_with_zinc and not row.has_zinc:
            return False
        if self._only_ro5 and not row.ro5_pass:
            return False
        return True

    # ------------------------------------------------------------------
    # Top-N: applied AFTER sort via mapToSource/sort iteration. The cleanest
    # way is to override ``rowCount`` on the proxy to clamp the visible count.
    # We also override ``index`` to keep mapping consistent.
    # ------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        n = super().rowCount(parent)
        if self._top_n is not None:
            n = min(n, max(0, int(self._top_n)))
        return n
