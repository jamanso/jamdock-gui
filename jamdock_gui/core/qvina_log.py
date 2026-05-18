"""Parser for QuickVina 2 / AutoDock Vina log output.

Each docking run writes a small text log with a fixed-format table::

    -----+------------+----------+----------
     mode |   affinity | dist from best mode
          | (kcal/mol) | rmsd l.b.| rmsd u.b.
    -----+------------+----------+----------
        1       -10.234      0.000      0.000
        2        -9.812      1.234      2.345
        3        -9.456      2.891      4.123
        ...

The columns are whitespace-aligned, but qvina02 doesn't always pad them
the same way, so we match each data row with a regex that's robust to
extra spaces and tabs. We also tolerate empty / aborted logs.

This module is reused by:
- :mod:`jamdock_gui.core.docking` — to extract the best score after each
  ligand finishes.
- Future Tab 4 (Results) — to compute SimScore from the per-mode RMSDs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# Match a single docking-mode row. Tolerates leading/trailing whitespace and
# both ``-7.812`` and ``  -7.8`` style values.
_MODE_ROW_RE = re.compile(
    r"^\s*(?P<mode>\d+)\s+"
    r"(?P<affinity>-?\d+\.\d+)\s+"
    r"(?P<lb>-?\d+\.\d+)\s+"
    r"(?P<ub>-?\d+\.\d+)\s*$"
)


@dataclass(frozen=True)
class DockingMode:
    """One row of the qvina output table."""

    mode: int           #: 1-based mode index (1 = best)
    affinity: float     #: kcal/mol; more negative is better
    rmsd_lb: float      #: lower-bound RMSD vs mode 1 (Å)
    rmsd_ub: float      #: upper-bound RMSD vs mode 1 (Å)


@dataclass(frozen=True)
class DockingLog:
    """Parsed contents of a qvina02 stdout/stderr log."""

    modes: tuple[DockingMode, ...]
    raw_path: Path | None
    error_text: str | None = None  #: best-effort message when no modes parsed

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        return not self.modes

    @property
    def n_modes(self) -> int:
        return len(self.modes)

    @property
    def best_score(self) -> float | None:
        """Affinity of mode 1, or ``None`` if the log has no modes."""
        return self.modes[0].affinity if self.modes else None

    def to_dict(self) -> dict:
        return {
            "n_modes": self.n_modes,
            "best_score": self.best_score,
            "modes": [
                {"mode": m.mode, "affinity": m.affinity,
                 "rmsd_lb": m.rmsd_lb, "rmsd_ub": m.rmsd_ub}
                for m in self.modes
            ],
            "error_text": self.error_text,
        }


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def parse_qvina_log(path: Path | str) -> DockingLog:
    """Parse a qvina02 log file.

    Never raises on malformed input — returns an empty :class:`DockingLog`
    with ``error_text`` set so the caller can surface it in the GUI.
    """
    p = Path(path)
    if not p.is_file():
        return DockingLog(modes=(), raw_path=p, error_text=f"log not found: {p}")

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return DockingLog(modes=(), raw_path=p, error_text=f"could not read log: {exc}")

    return parse_qvina_log_text(text, raw_path=p)


def parse_qvina_log_text(text: str, *, raw_path: Path | None = None) -> DockingLog:
    """Same as :func:`parse_qvina_log` but operates on an in-memory string."""
    modes: list[DockingMode] = []
    seen_table_header = False

    error_lines: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # Sniff the table header so we don't accidentally match a row
        # that looks numeric but is part of a banner.
        if stripped.startswith("mode") and "affinity" in stripped:
            seen_table_header = True
            continue
        if stripped.startswith("-----"):
            continue

        m = _MODE_ROW_RE.match(line)
        if m:
            try:
                modes.append(
                    DockingMode(
                        mode=int(m["mode"]),
                        affinity=float(m["affinity"]),
                        rmsd_lb=float(m["lb"]),
                        rmsd_ub=float(m["ub"]),
                    )
                )
            except ValueError:
                pass
            continue

        # Capture lines that look like errors so the GUI can surface them.
        lower = stripped.lower()
        if any(token in lower for token in ("error", "fail", "abort", "exception")):
            error_lines.append(stripped[:200])

    error_text = None
    if not modes:
        if error_lines:
            error_text = " | ".join(error_lines[:3])
        elif not seen_table_header:
            error_text = "no docking-mode table found"

    # qvina sometimes outputs modes out of order if the log got truncated;
    # sort by mode number so mode 1 (best) is always first.
    modes.sort(key=lambda x: x.mode)

    return DockingLog(modes=tuple(modes), raw_path=raw_path, error_text=error_text)
