"""Auto-detection of external binaries used by jamdock-suite."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from jamdock_gui.settings import Settings


@dataclass
class BinaryStatus:
    name: str
    path: str | None
    found: bool
    version: str | None = None
    notes: str = ""


def _which(name: str) -> str | None:
    return shutil.which(name)


def _try_version(args: list[str]) -> str | None:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
        text = (out.stdout or "") + (out.stderr or "")
        for line in text.splitlines():
            line = line.strip()
            if line:
                return line[:120]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return None


def probe_qvina02(settings: Settings) -> BinaryStatus:
    path = settings.get_binary(Settings.KEY_QVINA) or _which("qvina02") or _which("qvina2")
    if not path or not Path(path).exists():
        return BinaryStatus("qvina02", None, found=False, notes="Install QuickVina 2.")
    settings.set_binary(Settings.KEY_QVINA, path)
    return BinaryStatus("qvina02", path, found=True, version=_try_version([path, "--version"]))


def probe_obabel(settings: Settings) -> BinaryStatus:
    path = settings.get_binary(Settings.KEY_OBABEL) or _which("obabel")
    if not path or not Path(path).exists():
        return BinaryStatus("obabel", None, found=False,
                            notes="Install Open Babel (conda install -c conda-forge openbabel).")
    settings.set_binary(Settings.KEY_OBABEL, path)
    return BinaryStatus("obabel", path, found=True, version=_try_version([path, "-V"]))


def probe_fpocket(settings: Settings) -> BinaryStatus:
    path = settings.get_binary(Settings.KEY_FPOCKET) or _which("fpocket")
    if not path or not Path(path).exists():
        return BinaryStatus("fpocket", None, found=False,
                            notes="Install Fpocket (https://github.com/Discngine/fpocket).")
    settings.set_binary(Settings.KEY_FPOCKET, path)
    return BinaryStatus("fpocket", path, found=True, version=_try_version([path, "-h"]))


def probe_mgltools(settings: Settings) -> tuple[BinaryStatus, BinaryStatus]:
    pythonsh = settings.get_binary(Settings.KEY_PYTHONSH) or _which("pythonsh")
    prep = settings.get_binary(Settings.KEY_PREP_RECEPTOR)
    home = Path.home()
    if not pythonsh:
        for candidate in home.glob("Programs/mgltools_*/bin/pythonsh"):
            pythonsh = str(candidate); break
    if not prep:
        for candidate in home.glob(
            "Programs/mgltools_*/MGLToolsPckgs/AutoDockTools/Utilities24/prepare_receptor4.py"
        ):
            prep = str(candidate); break
    if pythonsh and Path(pythonsh).exists():
        settings.set_binary(Settings.KEY_PYTHONSH, pythonsh)
        py_status = BinaryStatus("pythonsh", pythonsh, found=True)
    else:
        py_status = BinaryStatus("pythonsh", None, found=False, notes="MGLTools not found.")
    if prep and Path(prep).exists():
        settings.set_binary(Settings.KEY_PREP_RECEPTOR, prep)
        prep_status = BinaryStatus("prepare_receptor4.py", prep, found=True)
    else:
        prep_status = BinaryStatus("prepare_receptor4.py", None, found=False,
            notes="Install MGLTools 1.5.7 from https://ccsb.scripps.edu/mgltools/")
    return py_status, prep_status


def probe_rdkit() -> BinaryStatus:
    try:
        import rdkit  # noqa: F401
        from rdkit import __version__ as v
        return BinaryStatus("rdkit (python)", "imported", found=True, version=v)
    except ImportError as e:
        return BinaryStatus("rdkit (python)", None, found=False, notes=str(e))


def probe_pdb2pqr(settings: Settings) -> BinaryStatus:
    path = (settings.get_binary(Settings.KEY_PDB2PQR)
            or _which("pdb2pqr30") or _which("pdb2pqr"))
    if not path or not Path(path).exists():
        return BinaryStatus("pdb2pqr", None, found=False,
            notes="Optional. Install with: pip install pdb2pqr")
    settings.set_binary(Settings.KEY_PDB2PQR, path)
    return BinaryStatus("pdb2pqr", path, found=True, version=_try_version([path, "--version"]))


def probe_pymol(settings: Settings) -> BinaryStatus:
    path = (settings.get_binary(Settings.KEY_PYMOL)
            or _which("pymol") or _which("PyMOL") or _which("pymol2"))
    if not path or not Path(path).exists():
        return BinaryStatus("pymol", None, found=False,
            notes="Optional. Install PyMOL for external 3D visualisation.")
    settings.set_binary(Settings.KEY_PYMOL, path)
    return BinaryStatus("pymol", path, found=True)


def probe_all(settings: Settings | None = None) -> dict[str, BinaryStatus]:
    settings = settings or Settings()
    qvina = probe_qvina02(settings)
    obabel = probe_obabel(settings)
    fpocket = probe_fpocket(settings)
    pythonsh, prep = probe_mgltools(settings)
    rdkit = probe_rdkit()
    pdb2pqr = probe_pdb2pqr(settings)
    pymol = probe_pymol(settings)
    return {
        "qvina02": qvina,
        "obabel": obabel,
        "fpocket": fpocket,
        "pythonsh": pythonsh,
        "prepare_receptor4": prep,
        "rdkit": rdkit,
        "pdb2pqr": pdb2pqr,
        "pymol": pymol,
    }


def missing_binaries(statuses: dict[str, BinaryStatus]) -> list[str]:
    return [name for name, st in statuses.items() if not st.found]
