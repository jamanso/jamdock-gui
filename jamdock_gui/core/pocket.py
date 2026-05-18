"""Parser for Fpocket output (the ``<base>_info.txt`` file).

Fpocket emits one block per pocket. Example excerpt::

    Pocket 1 :
            Score :                 0.732
            Druggability Score :    0.823
            Number of Alpha Spheres : 64
            Total SASA :            232.115
            Polar SASA :             93.247
            Apolar SASA :           138.868
            Volume :                890.452
            Mean local hydrophobic density : 28.182
            Mean alpha sphere radius :       4.157
            Mean alp. sph. solvent access :  0.481
            Apolar alpha sphere proportion : 0.547
            Hydrophobicity score :  35.733
            Volume score :           4.071
            Polarity score :         8
            Charge score :           1
            Flexibility :            0.234

    Pocket 2 :
            Score :                 0.310
            ...

We also expose :func:`read_pocket_atoms` to extract atomic coordinates from
``<out>/pockets/pocketN_atm.pdb`` — needed to compute the grid box.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Druggability cutoff that ``jamreceptor`` uses to flag a pocket as
#: "could be druggable". Reproduced verbatim.
DRUGGABLE_THRESHOLD: float = 0.15


@dataclass
class Pocket:
    """A single Fpocket pocket entry."""

    number: int
    score: float | None = None
    druggability_score: float | None = None
    n_alpha_spheres: int | None = None
    total_sasa: float | None = None
    polar_sasa: float | None = None
    apolar_sasa: float | None = None
    volume: float | None = None
    mean_local_hydrophobic_density: float | None = None
    mean_alpha_sphere_radius: float | None = None
    mean_alpha_sphere_solvent_acc: float | None = None
    apolar_alpha_sphere_proportion: float | None = None
    hydrophobicity_score: float | None = None
    volume_score: float | None = None
    polarity_score: float | None = None
    charge_score: float | None = None
    flexibility: float | None = None

    @property
    def is_druggable(self) -> bool:
        """``True`` if the druggability score exceeds the jamreceptor cut-off."""
        return self.druggability_score is not None and self.druggability_score > DRUGGABLE_THRESHOLD


# Map of substrings (as printed by Fpocket) → Pocket attribute name.
# Order matters: longer keys must come before shorter ones with the same prefix.
_FIELD_MAP: list[tuple[str, str, type]] = [
    # field-key (case sensitive, as in the file)            attribute name                       cast
    ("Druggability Score",                                  "druggability_score",                float),
    ("Number of Alpha Spheres",                             "n_alpha_spheres",                   int),
    ("Total SASA",                                          "total_sasa",                        float),
    ("Polar SASA",                                          "polar_sasa",                        float),
    ("Apolar SASA",                                         "apolar_sasa",                       float),
    ("Volume score",                                        "volume_score",                      float),
    ("Volume",                                              "volume",                            float),
    ("Mean local hydrophobic density",                      "mean_local_hydrophobic_density",    float),
    ("Mean alpha sphere radius",                            "mean_alpha_sphere_radius",          float),
    ("Mean alp. sph. solvent access",                       "mean_alpha_sphere_solvent_acc",     float),
    ("Apolar alpha sphere proportion",                      "apolar_alpha_sphere_proportion",    float),
    ("Hydrophobicity score",                                "hydrophobicity_score",              float),
    ("Polarity score",                                      "polarity_score",                    float),
    ("Charge score",                                        "charge_score",                      float),
    ("Flexibility",                                         "flexibility",                       float),
    # 'Score' is the most ambiguous — match it last so longer "*Score" keys win.
    ("Score",                                               "score",                             float),
]

_POCKET_HEADER_RE = re.compile(r"^Pocket\s+(\d+)\s*:")
_NUMBER_RE = re.compile(r":\s*(-?\d+(?:\.\d+)?)")


def parse_pocket_info(info_path: Path) -> list[Pocket]:
    """Parse ``<base>_info.txt`` produced by Fpocket and return a list of pockets.

    Returns:
        Pockets sorted by their pocket number (1, 2, 3, …).

    Raises:
        FileNotFoundError: if ``info_path`` does not exist.
    """
    info_path = Path(info_path)
    if not info_path.is_file():
        raise FileNotFoundError(info_path)

    pockets: list[Pocket] = []
    current: Pocket | None = None

    with open(info_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue

            header = _POCKET_HEADER_RE.match(line)
            if header:
                if current is not None:
                    pockets.append(current)
                current = Pocket(number=int(header.group(1)))
                continue

            if current is None:
                continue

            # Match the longest key first.
            for key, attr, cast in _FIELD_MAP:
                if line.startswith(key):
                    val = _NUMBER_RE.search(line)
                    if val:
                        try:
                            value: float | int = cast(float(val.group(1)))
                        except ValueError:
                            break
                        setattr(current, attr, value)
                    break

    if current is not None:
        pockets.append(current)

    pockets.sort(key=lambda p: p.number)
    return pockets


# ---------------------------------------------------------------------------
def read_pocket_atoms(pocket_atm_pdb: Path) -> list[tuple[float, float, float]]:
    """Extract Cartesian coordinates from ``pocketN_atm.pdb``.

    Fpocket writes pseudo-atoms decorated as ATOM lines; we only need the X/Y/Z.
    """
    pocket_atm_pdb = Path(pocket_atm_pdb)
    if not pocket_atm_pdb.is_file():
        raise FileNotFoundError(pocket_atm_pdb)

    coords: list[tuple[float, float, float]] = []
    with open(pocket_atm_pdb, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("ATOM"):
                continue
            if len(line) < 54:
                continue
            try:
                x = float(line[30:38])
                y = float(line[38:46])
                z = float(line[46:54])
            except ValueError:
                continue
            coords.append((x, y, z))
    return coords


# ---------------------------------------------------------------------------
def pocket_files(out_dir: Path, base_name: str) -> tuple[Path, Path]:
    """Return ``(info_txt, pockets_dir)`` paths under an Fpocket output dir."""
    out_dir = Path(out_dir)
    return out_dir / f"{base_name}_info.txt", out_dir / "pockets"
