"""Pure-Python port of the awk-based PDB cleanup in ``jamreceptor``.

The original bash logic (kept intact for CLI users):

.. code-block:: awk

    /^ATOM/ {
        resn  = substr(line, 18, 3)
        alt   = substr(line, 17, 1)
        chain = substr(line, 22, 1)
        if ((alt == " " || alt == "A") &&
            keep_chain[chain] &&
            resn in amino_acids) {
            print substr(line, 1, 16) " " substr(line, 18)   # clear ALTLOC
        }
    }

Why a Python port:
    - We need to detect the chains *before* asking the user which to keep.
    - We want instant feedback in the 3D viewer when the cleaned file is ready.
    - It's trivial to test in isolation.

PDB ATOM column layout (1-indexed) reminder, since the indexing is fiddly:

::

    1-6   record name "ATOM  "
    7-11  serial
    13-16 atom name
    17    altLoc
    18-20 residue name
    22    chain ID
    23-26 residue sequence
    31-38 x  / 39-46 y / 47-54 z
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: The 20 standard proteinogenic amino acids (3-letter codes).
STANDARD_AMINO_ACIDS: frozenset[str] = frozenset({
    "ALA", "ARG", "ASN", "ASP", "CYS",
    "GLU", "GLN", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO",
    "SER", "THR", "TRP", "TYR", "VAL",
})


@dataclass
class ChainInfo:
    """Summary of a chain found in a PDB file."""

    chain_id: str
    n_atoms: int
    n_residues: int
    is_protein: bool       #: at least one standard aa residue
    has_hetatm: bool       #: at least one HETATM line


@dataclass
class CleanReport:
    """Outcome of :func:`clean_pdb`."""

    input_path: Path
    output_path: Path
    chains_kept: list[str]
    n_atom_lines_input: int
    n_atom_lines_kept: int
    n_residues_kept: int


# ---------------------------------------------------------------------------
def detect_chains(pdb_path: Path) -> list[ChainInfo]:
    """Scan a PDB file and report which chains are present.

    The list is returned in the order the chains first appear in the file
    (so the GUI can show them in a sensible order). Both ATOM and HETATM
    are considered when determining ``has_hetatm``, but only ATOM lines
    feed ``n_atoms``/``n_residues``.
    """
    seen_order: list[str] = []
    atom_counts: dict[str, int] = {}
    residues_seen: dict[str, set[tuple[str, int, str]]] = {}
    is_protein: dict[str, bool] = {}
    has_hetatm: dict[str, bool] = {}

    with open(pdb_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not (line.startswith("ATOM") or line.startswith("HETATM")):
                continue
            if len(line) < 22:
                continue
            chain = line[21]
            if chain not in atom_counts:
                seen_order.append(chain)
                atom_counts[chain] = 0
                residues_seen[chain] = set()
                is_protein[chain] = False
                has_hetatm[chain] = False
            if line.startswith("ATOM"):
                atom_counts[chain] += 1
                resname = line[17:20].strip()
                try:
                    resseq = int(line[22:26])
                except ValueError:
                    resseq = -1
                icode = line[26] if len(line) > 26 else " "
                residues_seen[chain].add((resname, resseq, icode))
                if resname in STANDARD_AMINO_ACIDS:
                    is_protein[chain] = True
            else:
                has_hetatm[chain] = True

    return [
        ChainInfo(
            chain_id=c,
            n_atoms=atom_counts[c],
            n_residues=len(residues_seen[c]),
            is_protein=is_protein[c],
            has_hetatm=has_hetatm[c],
        )
        for c in seen_order
    ]


# ---------------------------------------------------------------------------
def clean_pdb(
    input_path: Path,
    output_path: Path,
    chains_to_keep: list[str] | tuple[str, ...] | set[str],
) -> CleanReport:
    """Filter *input_path* into *output_path* keeping only the selected chains.

    Behaviour mirrors the awk in jamreceptor:

    1. Only ``ATOM`` records are emitted (no HETATM, no waters, no ions).
    2. Only the 20 standard amino acids are kept.
    3. ALTLOC must be either blank or ``A``.
    4. The ALTLOC column is cleared in the output (overwritten with a space).

    Raises:
        FileNotFoundError: if ``input_path`` does not exist.
        ValueError: if the cleaned file would be empty.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    keep = set(chains_to_keep)
    if not keep:
        raise ValueError("chains_to_keep must contain at least one chain ID")

    if not input_path.is_file():
        raise FileNotFoundError(input_path)

    n_in = 0
    n_out = 0
    residues_out: set[tuple[str, str, int, str]] = set()

    with open(input_path, "r", encoding="utf-8", errors="replace") as fin, \
         open(output_path, "w", encoding="utf-8") as fout:
        for line in fin:
            if not line.startswith("ATOM"):
                continue
            n_in += 1
            if len(line) < 27:
                continue

            altloc = line[16]
            resname = line[17:20]
            chain = line[21]

            if altloc not in (" ", "A"):
                continue
            if chain not in keep:
                continue
            if resname.strip() not in STANDARD_AMINO_ACIDS:
                continue

            # Clear ALTLOC: keep cols 1-16, force col 17 to ' ', then cols 18+.
            cleaned = line[:16] + " " + line[17:]
            fout.write(cleaned)
            n_out += 1
            try:
                resseq = int(line[22:26])
            except ValueError:
                resseq = -1
            icode = line[26]
            residues_out.add((chain, resname.strip(), resseq, icode))

    if n_out == 0:
        raise ValueError(
            f"Cleaned PDB would be empty (no ATOM lines matched the filter "
            f"for chains={sorted(keep)} in {input_path})."
        )

    return CleanReport(
        input_path=input_path,
        output_path=output_path,
        chains_kept=sorted(keep),
        n_atom_lines_input=n_in,
        n_atom_lines_kept=n_out,
        n_residues_kept=len(residues_out),
    )
