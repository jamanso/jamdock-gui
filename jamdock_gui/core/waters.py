"""Structural-water detection and preservation for receptor preparation.

Why this module exists
----------------------
``jamreceptor``'s default workflow strips every water in the receptor (the
awk filter only emits ATOM lines, and ``prepare_receptor4 -U …waters…``
removes anything that survives). That's fine for fast virtual screening
but it sacrifices a known accuracy boost: bridging waters in the binding
site often form H-bonds with both protein and ligand, contributing
~1-2 kcal/mol that the docking score gets right only if those waters
are still part of the receptor.

This module lets the GUI:
  * **detect** waters in the original PDB,
  * **score** each water against four cheap structural criteria,
  * **filter** automatically by the user-tunable thresholds, and
  * **inject** the kept waters back into the cleaned PDB so they survive
    through the rest of the pipeline.

No external dependencies — pure Python, fully testable offline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
#: HETATM residue names recognised as waters. ``HOH`` is by far the most
#: common; ``WAT``, ``H2O`` and ``DOD`` (heavy water) appear in some PDBs.
WATER_RESNAMES: frozenset[str] = frozenset({"HOH", "WAT", "H2O", "DOD"})

#: HETATM residue names treated as ions (excluded from "ligand" distance).
#: Just the heavy ones we care about — common cofactors in metalloproteins.
ION_RESNAMES: frozenset[str] = frozenset({
    "NA", "CL", "BR", "K", "MG", "CA", "ZN", "FE", "CU", "MN",
    "CO", "NI", "HG", "CD", "IOD", "F", "FE2", "FE3",
})


@dataclass
class WaterCriteria:
    """User-tunable thresholds for the automatic "structural water" filter.

    A water passes when **all of the enabled rules** are satisfied. The
    ``use_*`` flags let the GUI toggle individual criteria without
    re-running anything else.
    """

    # B-factor (Å²) — low B → ordered → likely structural.
    use_b_factor: bool = True
    max_b_factor: float = 30.0

    # Occupancy — partial waters are unreliable.
    use_occupancy: bool = True
    min_occupancy: float = 0.5

    # Number of polar protein atoms (N/O) within H-bond distance.
    use_polar_neighbors: bool = True
    min_polar_neighbors: int = 2
    polar_neighbor_distance: float = 3.5      # Å (canonical H-bond cutoff)

    # Distance to bound ligand (or grid-box centre, fallback).
    # Used to focus on binding-site waters; outside this radius they're
    # bulk solvent and rarely useful for docking.
    use_distance_to_ligand: bool = True
    max_distance_to_ligand: float = 5.0       # Å

    #: How many of the four enabled criteria a water must meet to be
    #: considered structural. With 4 criteria active and a strict cutoff
    #: of 4, only waters satisfying every rule pass.
    min_score_to_keep: int = 3


# ---------------------------------------------------------------------------
# Water record
# ---------------------------------------------------------------------------
@dataclass
class Water:
    """One water molecule (oxygen heavy atom) extracted from the PDB."""

    # Identification
    line_index: int                 #: 0-based index of the source HETATM line
    raw_line: str                   #: original PDB line, kept for re-injection
    chain: str
    resseq: int
    icode: str

    # Coordinates
    x: float
    y: float
    z: float

    # Quality metrics from the PDB
    b_factor: float
    occupancy: float

    # Derived (filled in by :func:`score_waters`)
    n_polar_neighbors: int = 0
    dist_to_ligand: float | None = None
    structural_score: int = 0           #: 0..4, number of criteria met
    is_structural: bool = False         #: structural_score >= criteria.min_score_to_keep

    @property
    def label(self) -> str:
        """Human-readable identifier — e.g. ``A:401``."""
        return f"{self.chain}:{self.resseq}{self.icode.strip()}"

    @property
    def coords(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


# ---------------------------------------------------------------------------
# Lightweight PDB helpers (no Bio.PDB dep)
# ---------------------------------------------------------------------------
def _parse_xyz(line: str) -> tuple[float, float, float] | None:
    if len(line) < 54:
        return None
    try:
        return float(line[30:38]), float(line[38:46]), float(line[46:54])
    except ValueError:
        return None


def _is_polar_protein_atom(atom_name: str) -> bool:
    """True for nitrogen/oxygen heavy atoms in standard amino acids."""
    name = atom_name.strip()
    return bool(name) and name[0] in ("N", "O")


def _sq_distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    dx, dy, dz = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return dx * dx + dy * dy + dz * dz


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def find_waters(pdb_path: Path | str) -> list[Water]:
    """Return every water HETATM record in *pdb_path* (oxygen-only).

    We dedupe by (chain, resseq, icode) so a single water molecule with
    explicit hydrogens (rare in deposited X-ray structures, common in NMR
    ensembles) is reported once.
    """
    p = Path(pdb_path)
    if not p.is_file():
        raise FileNotFoundError(p)

    seen: set[tuple[str, int, str]] = set()
    out: list[Water] = []

    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for i, raw in enumerate(fh):
            line = raw.rstrip("\r\n")
            if not line.startswith("HETATM"):
                continue
            if len(line) < 54:
                continue

            resname = line[17:20].strip().upper()
            if resname not in WATER_RESNAMES:
                continue

            atom_name = line[12:16].strip()
            # Skip explicit hydrogens on water — we only want the oxygen.
            if atom_name and atom_name[0] == "H":
                continue

            chain = line[21]
            try:
                resseq = int(line[22:26])
            except ValueError:
                continue
            icode = line[26] if len(line) > 26 else " "
            key = (chain, resseq, icode)
            if key in seen:
                continue
            seen.add(key)

            xyz = _parse_xyz(line)
            if xyz is None:
                continue

            try:
                occ = float(line[54:60]) if len(line) >= 60 else 1.0
            except ValueError:
                occ = 1.0
            try:
                bfac = float(line[60:66]) if len(line) >= 66 else 0.0
            except ValueError:
                bfac = 0.0

            out.append(Water(
                line_index=i,
                raw_line=line,
                chain=chain,
                resseq=resseq,
                icode=icode,
                x=xyz[0], y=xyz[1], z=xyz[2],
                b_factor=bfac,
                occupancy=occ,
            ))
    return out


def _iter_polar_protein_coords(
    pdb_path: Path | str,
    chains_kept: set[str] | None = None,
) -> list[tuple[float, float, float]]:
    """Heavy N/O atoms from ATOM records (optionally filtered to chains)."""
    p = Path(pdb_path)
    out: list[tuple[float, float, float]] = []
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line.startswith("ATOM"):
                continue
            if len(line) < 54:
                continue
            if chains_kept is not None and line[21] not in chains_kept:
                continue
            atom_name = line[12:16]
            if not _is_polar_protein_atom(atom_name):
                continue
            xyz = _parse_xyz(line)
            if xyz is not None:
                out.append(xyz)
    return out


def _iter_ligand_coords(pdb_path: Path | str) -> list[tuple[float, float, float]]:
    """HETATM coords for atoms that are neither water nor ion (= bound ligand)."""
    p = Path(pdb_path)
    out: list[tuple[float, float, float]] = []
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line.startswith("HETATM"):
                continue
            if len(line) < 54:
                continue
            resname = line[17:20].strip().upper()
            if resname in WATER_RESNAMES or resname in ION_RESNAMES:
                continue
            xyz = _parse_xyz(line)
            if xyz is not None:
                out.append(xyz)
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_waters(
    waters: list[Water],
    pdb_path: Path | str,
    *,
    chains_kept: set[str] | None = None,
    fallback_anchor: tuple[float, float, float] | None = None,
    criteria: WaterCriteria | None = None,
) -> None:
    """Fill ``n_polar_neighbors``, ``dist_to_ligand`` and the structural flags.

    *pdb_path* is the same PDB we read the waters from — used to compute
    polar-neighbor counts and ligand distances.

    *fallback_anchor* is consulted when the PDB has no HETATM ligand (i.e.
    no bound substrate to anchor the "binding-site" criterion). Typical
    use: pass the grid-box centre from Step 4. If both are unavailable,
    the ligand-distance criterion is skipped automatically.
    """
    crit = criteria or WaterCriteria()

    polar_coords = _iter_polar_protein_coords(pdb_path, chains_kept=chains_kept)
    ligand_coords = _iter_ligand_coords(pdb_path)
    if not ligand_coords and fallback_anchor is not None:
        ligand_coords = [fallback_anchor]
    have_ligand_anchor = bool(ligand_coords)

    polar_cutoff_sq = crit.polar_neighbor_distance ** 2

    for w in waters:
        wc = (w.x, w.y, w.z)

        # 1) Polar neighbours (cheap O(n) scan; protein atoms typically ~3k).
        if crit.use_polar_neighbors:
            n_nb = 0
            for pc in polar_coords:
                if _sq_distance(wc, pc) <= polar_cutoff_sq:
                    n_nb += 1
                    # We only need to know if we hit the threshold; once we
                    # reach 2× the requested minimum the count stops mattering.
                    if n_nb >= crit.min_polar_neighbors * 2:
                        break
            w.n_polar_neighbors = n_nb
        else:
            w.n_polar_neighbors = 0

        # 2) Distance to the closest non-water HETATM (ligand or anchor).
        if have_ligand_anchor:
            best_sq = min(_sq_distance(wc, lc) for lc in ligand_coords)
            w.dist_to_ligand = math.sqrt(best_sq)
        else:
            w.dist_to_ligand = None

        # 3) Combined score.
        score = 0
        if crit.use_b_factor and w.b_factor < crit.max_b_factor:
            score += 1
        if crit.use_occupancy and w.occupancy >= crit.min_occupancy:
            score += 1
        if crit.use_polar_neighbors and w.n_polar_neighbors >= crit.min_polar_neighbors:
            score += 1
        if crit.use_distance_to_ligand and have_ligand_anchor:
            if w.dist_to_ligand is not None and w.dist_to_ligand <= crit.max_distance_to_ligand:
                score += 1

        w.structural_score = score
        w.is_structural = score >= crit.min_score_to_keep


def filter_structural(waters: list[Water]) -> list[Water]:
    """Return only waters whose ``is_structural`` flag is True.

    Run :func:`score_waters` first to populate the flag.
    """
    return [w for w in waters if w.is_structural]


# ---------------------------------------------------------------------------
# Injection — add the kept waters back into the cleaned PDB
# ---------------------------------------------------------------------------
def inject_waters_into_pdb(
    cleaned_pdb: Path | str,
    waters_to_keep: list[Water],
    output_pdb: Path | str | None = None,
) -> Path:
    """Append HETATM records for *waters_to_keep* to *cleaned_pdb*.

    The cleaned PDB (produced by ``core.pdb_clean.clean_pdb``) only has
    ATOM lines of standard amino acids. We append the original HETATM
    lines of the kept waters right before the trailing ``END`` (or at the
    end of file if no END marker is present).

    Args:
        cleaned_pdb: Source PDB (will be left untouched unless
            *output_pdb* is None, in which case it's overwritten).
        waters_to_keep: List of :class:`Water` objects whose ``raw_line``
            will be copied verbatim.
        output_pdb: Destination path. Defaults to overwriting *cleaned_pdb*.

    Returns:
        Path of the written file.
    """
    src = Path(cleaned_pdb)
    dst = Path(output_pdb) if output_pdb is not None else src
    if not src.is_file():
        raise FileNotFoundError(src)
    if not waters_to_keep:
        # Nothing to inject — just copy through if dst != src.
        if dst != src:
            dst.write_text(src.read_text(encoding="utf-8", errors="replace"))
        return dst

    body_lines: list[str] = []
    end_line: str | None = None
    with src.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if line.strip().upper() == "END":
                end_line = line
                continue
            body_lines.append(line)

    with dst.open("w", encoding="utf-8") as fh:
        for ln in body_lines:
            fh.write(ln + "\n")
        # Append a separator comment for readability.
        fh.write("REMARK   1 Structural waters preserved by jamdock-gui:\n")
        for w in waters_to_keep:
            fh.write(
                f"REMARK   1   {w.label}  B={w.b_factor:.1f}  "
                f"occ={w.occupancy:.2f}  nbrs={w.n_polar_neighbors}\n"
            )
        for w in waters_to_keep:
            fh.write(w.raw_line + "\n")
        if end_line is not None:
            fh.write(end_line + "\n")
    return dst
