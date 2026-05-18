"""Results aggregator and per-ligand metrics — replaces ``jamrank``.

Architecture
------------
- :class:`ResultRow` is the per-ligand record consumed by the table model.
- Pure helpers (``compute_sim_score``, ``extract_zinc_id``, ``compute_mw``)
  are testable without Qt.
- :class:`ResultsAggregator` (a ``QObject``) is the live updater: it can be
  fed log paths one by one (when Tab 3 emits ``job_finished``) or do a bulk
  scan of a ``docking_results/`` folder (initial load / resume / reload).

SimScore (jamrank Option 2 ranking)
-----------------------------------
Identical maths as the bash::

    pct_lb = 100 * (count_lb_under_1.6 - 1) / total_modes
    pct_ub = 100 * (count_ub_under_3.2 - 1) / total_modes
    SimScore = round((pct_lb + pct_ub) / 2)

It's a proxy for pose-convergence: 100% means every alternative mode is
within 1.6 / 3.2 Å RMSD of the best mode, i.e. the docking strongly
prefers a single binding pose.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from jamdock_gui.core.qvina_log import (
    DockingLog,
    DockingMode,
    parse_qvina_log,
)

log = logging.getLogger(__name__)

ZINC_BASE_URL = "https://zinc.docking.org/substances/"

# RDKit / openbabel are best-effort. We fall back gracefully when neither
# is available (the user just won't see MW in the table).
try:
    from rdkit import Chem, RDLogger
    from rdkit.Chem import Crippen, Descriptors, Lipinski
    # Silence ALL of RDKit's C++ logger output. The two main offenders are:
    #   * "tagged 2D but Z != 0" advisory warnings — RDKit auto-corrects them
    #   * "Explicit valence for atom # N N, 4, is greater than permitted"
    #     errors when reading SDFs that store quaternary nitrogens without
    #     explicit formal charges. SDMolSupplier just returns None for those
    #     molecules and we handle the None — the screenful of red errors
    #     piped to stderr is pure noise that scared users into thinking
    #     something was broken on re-open. Killing rdApp.* nukes them all.
    RDLogger.DisableLog("rdApp.*")
    _HAS_RDKIT = True
except ImportError:
    Chem = None  # type: ignore
    Crippen = None  # type: ignore
    Descriptors = None  # type: ignore
    Lipinski = None  # type: ignore
    _HAS_RDKIT = False


# ---------------------------------------------------------------------------
# Lipinski's Rule of Five — drug-likeness flag
# ---------------------------------------------------------------------------
RO5_MW_MAX = 500.0    # Da
RO5_LOGP_MAX = 5.0
RO5_HBD_MAX = 5
RO5_HBA_MAX = 10


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class ResultRow:
    """One row of the Results & Analysis table."""

    ligand: str                          #: filename stem, e.g. "412"
    log_path: Path
    pose_path: Path                      #: <ligand>_docking.pdbqt
    sdf_path: Path | None = None         #: companion SDF if found
    affinity: float | None = None        #: best-mode kcal/mol
    sim_score: int | None = None         #: 0–100
    n_modes: int = 0
    mw: float | None = None
    logp: float | None = None            #: Crippen logP (RDKit)
    hbd: int | None = None               #: H-bond donors (RDKit Lipinski)
    hba: int | None = None               #: H-bond acceptors (RDKit Lipinski)
    ro5_violations: int | None = None    #: 0..4
    ro5_pass: bool | None = None         #: True iff 0 violations
    zinc_id: str | None = None
    zinc_link: str | None = None
    error: str | None = None             #: when the log couldn't be parsed
    descriptors: dict[str, float] = field(default_factory=dict)  #: extensible

    @property
    def is_druggable(self) -> bool:
        """Heuristic — kept for symmetry with Pocket; not used for ranking."""
        return self.affinity is not None and self.affinity <= -7.0

    @property
    def has_zinc(self) -> bool:
        return bool(self.zinc_id)


# ---------------------------------------------------------------------------
# SimScore
# ---------------------------------------------------------------------------
RMSD_LB_CUTOFF = 1.6   # Å — same as jamrank
RMSD_UB_CUTOFF = 3.2   # Å — same as jamrank


def compute_sim_score(modes: list[DockingMode] | tuple[DockingMode, ...]) -> int | None:
    """Reproduce jamrank's SimScore (Option 2 ranking) byte-for-byte.

    Returns:
        Integer 0..100, or ``None`` if there are no modes.
    """
    total = len(modes)
    if total == 0:
        return None
    cnt_lb = sum(1 for m in modes if 0.0 <= m.rmsd_lb < RMSD_LB_CUTOFF)
    cnt_ub = sum(1 for m in modes if 0.0 <= m.rmsd_ub < RMSD_UB_CUTOFF)
    pct_lb = 100.0 * (cnt_lb - 1) / total
    pct_ub = 100.0 * (cnt_ub - 1) / total
    return int(round((pct_lb + pct_ub) / 2.0))


# ---------------------------------------------------------------------------
# ZINC ID extraction
# ---------------------------------------------------------------------------
_ZINC_TAG_RE = re.compile(r"<\s*ZINC[_-]?ID\s*>", re.IGNORECASE)
_ZINC_FULL_RE = re.compile(r"^\s*(ZINC\d+)\s*$", re.IGNORECASE)


def extract_zinc_id(sdf_path: Path | str) -> str | None:
    """Extract a ZINC ID from an SDF file.

    Strategy (matches jamrank):

    1. Look for a tag block ``> <ZINC_ID>\\nZINCxxxxxx``.
    2. Failing that, check whether the very first line of the file is a
       bare ``ZINCxxxxxx``.
    """
    p = Path(sdf_path)
    if not p.is_file():
        return None
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            first_line: str | None = None
            tag_seen = False
            for raw in fh:
                line = raw.strip()
                if first_line is None:
                    first_line = line
                if tag_seen and line:
                    # Some SDFs prefix with ``ZINC`` only; whitespace already stripped.
                    if line.startswith("ZINC") and line[4:].lstrip("0123456789") == "":
                        return line
                    tag_seen = False  # tag-block lines must be immediate
                if _ZINC_TAG_RE.search(line):
                    tag_seen = True
            if first_line:
                m = _ZINC_FULL_RE.match(first_line)
                if m:
                    return m.group(1)
    except OSError:
        return None
    return None


def zinc_link(zinc_id: str | None) -> str | None:
    if not zinc_id:
        return None
    return f"{ZINC_BASE_URL}{zinc_id}/"


# ---------------------------------------------------------------------------
# Molecular weight (RDKit primary, no obabel fallback to keep deps tight)
# ---------------------------------------------------------------------------
def compute_mw(sdf_path: Path | str) -> float | None:
    """Return monoisotopic / exact MW (Da) using RDKit, or ``None`` if unavailable."""
    if not _HAS_RDKIT:
        return None
    p = Path(sdf_path)
    if not p.is_file():
        return None
    try:
        suppl = Chem.SDMolSupplier(str(p), removeHs=False)
        for mol in suppl:
            if mol is None:
                continue
            try:
                return float(Descriptors.MolWt(mol))
            except Exception:
                continue
    except Exception:
        return None
    return None


def compute_lipinski(sdf_path: Path | str) -> dict | None:
    """Compute MW + LogP + HBD + HBA + Ro5 verdict for the first molecule in *sdf_path*.

    Returns a dict::

        {"mw": 412.5, "logp": 3.2, "hbd": 2, "hba": 5,
         "ro5_violations": 0, "ro5_pass": True}

    Or ``None`` if RDKit isn't installed / the file is unreadable / no parsable mol.

    Lipinski's "rule of five" (J. Med. Chem. 1997):
        MW ≤ 500, LogP ≤ 5, HBD ≤ 5, HBA ≤ 10.
    Pass = 0 violations. The dict reports the exact count so the GUI can
    show "passes with 1 violation" tooltips for the lenient interpretation.
    """
    if not _HAS_RDKIT:
        return None
    p = Path(sdf_path)
    if not p.is_file():
        return None
    try:
        suppl = Chem.SDMolSupplier(str(p), removeHs=False)
        mol = None
        for candidate in suppl:
            if candidate is not None:
                mol = candidate
                break
        if mol is None:
            return None
        try:
            mw = float(Descriptors.MolWt(mol))
            logp = float(Crippen.MolLogP(mol))
            hbd = int(Lipinski.NumHDonors(mol))
            hba = int(Lipinski.NumHAcceptors(mol))
        except Exception:
            return None
        violations = (
            int(mw   > RO5_MW_MAX)
            + int(logp > RO5_LOGP_MAX)
            + int(hbd  > RO5_HBD_MAX)
            + int(hba  > RO5_HBA_MAX)
        )
        return {
            "mw": mw, "logp": logp, "hbd": hbd, "hba": hba,
            "ro5_violations": violations,
            "ro5_pass": violations == 0,
        }
    except Exception:
        return None


def ro5_violation_details(row: "ResultRow") -> list[str]:
    """Human-readable list of which rules a row violates (empty if it passes)."""
    out: list[str] = []
    if row.mw is not None and row.mw > RO5_MW_MAX:
        out.append(f"MW {row.mw:.0f} > {RO5_MW_MAX:.0f}")
    if row.logp is not None and row.logp > RO5_LOGP_MAX:
        out.append(f"LogP {row.logp:.2f} > {RO5_LOGP_MAX:.0f}")
    if row.hbd is not None and row.hbd > RO5_HBD_MAX:
        out.append(f"HBD {row.hbd} > {RO5_HBD_MAX}")
    if row.hba is not None and row.hba > RO5_HBA_MAX:
        out.append(f"HBA {row.hba} > {RO5_HBA_MAX}")
    return out


# ---------------------------------------------------------------------------
# Companion SDF discovery — given the docking pose path, find the input SDF.
# ---------------------------------------------------------------------------
def find_companion_sdf(workdir: Path, ligand_stem: str) -> Path | None:
    """Locate the input ``<stem>.sdf`` produced by jamlib.

    jamlib writes ``library_sdf_<N>/<stem>.sdf`` for custom libraries and
    ``fda_sdf_compounds/<stem>.sdf`` for the FDA library.
    """
    candidates = [workdir / "fda_sdf_compounds"]
    candidates.extend(sorted(workdir.glob("library_sdf_*")))
    for d in candidates:
        if d.is_dir():
            sdf = d / f"{ligand_stem}.sdf"
            if sdf.is_file():
                return sdf
    return None


# ---------------------------------------------------------------------------
# Main entry: build a ResultRow from a finished docking
# ---------------------------------------------------------------------------
def build_result_row(
    log_path: Path | str,
    *,
    workdir: Path,
    pose_path: Path | None = None,
    enrich_chem: bool = True,
) -> ResultRow:
    """Parse one log + (optionally) extract chem metadata, return a row.

    ``enrich_chem=False`` skips RDKit / SDF reads — useful for quick bulk
    scans where only Affinity/SimScore are needed up-front.
    """
    log_p = Path(log_path)
    stem = log_p.stem.replace("_docking.pdbqt", "")
    if pose_path is None:
        pose_path = log_p.with_suffix("")  # strip ``.log`` → ``<stem>_docking.pdbqt``

    parsed: DockingLog = parse_qvina_log(log_p)

    row = ResultRow(
        ligand=stem,
        log_path=log_p,
        pose_path=pose_path,
        affinity=parsed.best_score,
        sim_score=compute_sim_score(parsed.modes),
        n_modes=parsed.n_modes,
        error=parsed.error_text if parsed.is_empty else None,
    )

    if enrich_chem:
        sdf = find_companion_sdf(workdir, stem)
        row.sdf_path = sdf
        if sdf:
            row.zinc_id = extract_zinc_id(sdf)
            row.zinc_link = zinc_link(row.zinc_id)
            lip = compute_lipinski(sdf)
            if lip is not None:
                row.mw = lip["mw"]
                row.logp = lip["logp"]
                row.hbd = lip["hbd"]
                row.hba = lip["hba"]
                row.ro5_violations = lip["ro5_violations"]
                row.ro5_pass = lip["ro5_pass"]
            else:
                # Fallback: at least try MW alone (no RDKit installed → both None).
                row.mw = compute_mw(sdf)

    return row


# ---------------------------------------------------------------------------
# Live aggregator — bridges Tab 3's signals to Tab 4's table
# ---------------------------------------------------------------------------
class ResultsAggregator(QObject):
    """Maintains the canonical list of :class:`ResultRow` for a workdir.

    Signals
    -------
    row_added:    ``ResultRow`` — emitted whenever a new ligand is parsed.
    row_updated:  ``ResultRow`` — emitted when a re-parse changes an existing row.
    bulk_loaded:  ``int`` — total rows after a bulk scan.
    """

    row_added = Signal(object)        # ResultRow
    row_updated = Signal(object)      # ResultRow
    bulk_loaded = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._workdir: Path | None = None
        self._rows: dict[str, ResultRow] = {}   # ligand_stem -> row

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_workdir(self, workdir: Path | str | None) -> None:
        if workdir is None:
            self._workdir = None
        else:
            self._workdir = Path(workdir)

    @property
    def rows(self) -> list[ResultRow]:
        return list(self._rows.values())

    def __len__(self) -> int:
        return len(self._rows)

    def clear(self) -> None:
        self._rows.clear()

    def get(self, ligand_stem: str) -> ResultRow | None:
        return self._rows.get(ligand_stem)

    # ------------------------------------------------------------------
    # Live ingestion (one ligand at a time)
    # ------------------------------------------------------------------
    def ingest_log(self, log_path: Path | str) -> ResultRow | None:
        """Parse *log_path* and add/update the row. Emits the right signal.

        Returns the row, or ``None`` if there's no working directory set.
        """
        if not self._workdir:
            return None
        log_p = Path(log_path)
        if not log_p.is_file():
            return None
        try:
            row = build_result_row(log_p, workdir=self._workdir, enrich_chem=True)
        except Exception as exc:                # pragma: no cover - defensive
            log.warning("ResultsAggregator.ingest_log failed for %s: %s", log_p, exc)
            return None

        existing = self._rows.get(row.ligand)
        self._rows[row.ligand] = row
        if existing is None:
            self.row_added.emit(row)
        else:
            self.row_updated.emit(row)
        return row

    # ------------------------------------------------------------------
    # Bulk scan (initial load / reload button)
    # ------------------------------------------------------------------
    def scan_workdir(self) -> int:
        """Re-parse every ``*.log`` under ``<workdir>/docking_results/``.

        Replaces any cached rows; emits :attr:`bulk_loaded` with the final
        row count.
        """
        if not self._workdir:
            return 0
        results_dir = self._workdir / "docking_results"
        if not results_dir.is_dir():
            self._rows.clear()
            self.bulk_loaded.emit(0)
            return 0

        new_rows: dict[str, ResultRow] = {}
        for log_path in sorted(results_dir.glob("*.log")):
            try:
                row = build_result_row(log_path, workdir=self._workdir)
                new_rows[row.ligand] = row
            except Exception as exc:
                log.warning("scan_workdir: skipped %s (%s)", log_path, exc)
        self._rows = new_rows
        self.bulk_loaded.emit(len(self._rows))
        return len(self._rows)
