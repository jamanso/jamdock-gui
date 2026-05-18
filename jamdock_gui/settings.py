"""Persistent settings wrapper around ``QSettings``.

Centralises all user-tunable knobs:
- paths to external binaries (``qvina02``, ``obabel``, ``pythonsh``, ``fpocket``)
- overrides for the bundled bash scripts (jamlib, jamreceptor, ...)
- defaults for the docking parameters (exhaustiveness, num_modes, etc.)
- last-used working directory
- last-used MW/LogP ranges for jamlib
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSettings


@dataclass
class DockingDefaults:
    exhaustiveness: int = 8
    num_modes: int = 9
    energy_range: float = 3.0
    cpu_per_job: int = 4
    parallel_jobs: int = max(1, (os.cpu_count() or 4) // 4)


@dataclass
class LibraryDefaults:
    mw_min: int = 300
    mw_max: int = 450
    logp_min: float = -1.0
    logp_max: float = 2.0
    n_compounds: int = 1000


class Settings:
    """Thin wrapper over QSettings with typed accessors."""

    # -- binary paths -----------------------------------------------------
    KEY_QVINA = "binaries/qvina02"
    KEY_OBABEL = "binaries/obabel"
    KEY_FPOCKET = "binaries/fpocket"
    KEY_PYTHONSH = "binaries/pythonsh"
    KEY_PREP_RECEPTOR = "binaries/prepare_receptor4"
    KEY_MGLTOOLS_DIR = "binaries/mgltools_dir"
    KEY_PDB2PQR = "binaries/pdb2pqr"
    KEY_PYMOL = "binaries/pymol"

    # -- bundled-script overrides ----------------------------------------
    KEY_SCRIPT_JAMLIB = "scripts/jamlib"
    KEY_SCRIPT_JAMRECEPTOR = "scripts/jamreceptor"
    KEY_SCRIPT_JAMQVINA = "scripts/jamqvina"
    KEY_SCRIPT_JAMRANK = "scripts/jamrank"
    KEY_SCRIPT_JAMRESUME = "scripts/jamresume"

    SCRIPT_KEYS = {
        "jamlib": KEY_SCRIPT_JAMLIB,
        "jamreceptor": KEY_SCRIPT_JAMRECEPTOR,
        "jamqvina": KEY_SCRIPT_JAMQVINA,
        "jamrank": KEY_SCRIPT_JAMRANK,
        "jamresume": KEY_SCRIPT_JAMRESUME,
    }

    # -- workspace --------------------------------------------------------
    KEY_LAST_WORKDIR = "workspace/last_workdir"
    KEY_RECENT_WORKDIRS = "workspace/recent_workdirs"

    # -- docking defaults -------------------------------------------------
    KEY_DOCK_EXH = "docking/exhaustiveness"
    KEY_DOCK_MODES = "docking/num_modes"
    KEY_DOCK_ENERGY = "docking/energy_range"
    KEY_DOCK_CPU = "docking/cpu_per_job"
    KEY_DOCK_PARALLEL = "docking/parallel_jobs"

    # -- library defaults -------------------------------------------------
    KEY_LIB_MW_MIN = "library/mw_min"
    KEY_LIB_MW_MAX = "library/mw_max"
    KEY_LIB_LOGP_MIN = "library/logp_min"
    KEY_LIB_LOGP_MAX = "library/logp_max"
    KEY_LIB_N = "library/n_compounds"

    # -- misc -------------------------------------------------------------
    KEY_FIRST_RUN = "misc/first_run"
    KEY_SHOW_CITATIONS = "misc/show_citations"

    def __init__(self) -> None:
        self._qs = QSettings()

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------
    def _get(self, key: str, default: object = None, type_: type | None = None) -> object:
        if type_ is not None:
            return self._qs.value(key, default, type=type_)
        return self._qs.value(key, default)

    def _set(self, key: str, value: object) -> None:
        self._qs.setValue(key, value)
        self._qs.sync()

    # ------------------------------------------------------------------
    # Binary paths
    # ------------------------------------------------------------------
    def get_binary(self, key: str) -> str | None:
        val = self._get(key, None)
        return str(val) if val else None

    def set_binary(self, key: str, path: str | Path | None) -> None:
        self._set(key, str(path) if path else "")

    # ------------------------------------------------------------------
    # Script overrides
    # ------------------------------------------------------------------
    def get_script_override(self, name: str) -> str | None:
        key = self.SCRIPT_KEYS.get(name)
        if not key:
            return None
        val = self._get(key, None)
        return str(val) if val else None

    def set_script_override(self, name: str, path: str | Path | None) -> None:
        key = self.SCRIPT_KEYS.get(name)
        if key:
            self._set(key, str(path) if path else "")

    # ------------------------------------------------------------------
    # Workspace
    # ------------------------------------------------------------------
    def last_workdir(self) -> Path | None:
        v = self._get(self.KEY_LAST_WORKDIR, None)
        return Path(str(v)) if v else None

    def set_last_workdir(self, path: Path | str) -> None:
        path = Path(path)
        self._set(self.KEY_LAST_WORKDIR, str(path))
        recents = self.recent_workdirs()
        recents = [p for p in recents if p != path]
        recents.insert(0, path)
        recents = recents[:8]
        self._set(self.KEY_RECENT_WORKDIRS, [str(p) for p in recents])

    def recent_workdirs(self) -> list[Path]:
        raw = self._get(self.KEY_RECENT_WORKDIRS, [])
        if not raw:
            return []
        if isinstance(raw, str):
            raw = [raw]
        return [Path(str(p)) for p in raw if Path(str(p)).exists()]

    # ------------------------------------------------------------------
    # Docking defaults
    # ------------------------------------------------------------------
    def docking_defaults(self) -> DockingDefaults:
        return DockingDefaults(
            exhaustiveness=int(self._get(self.KEY_DOCK_EXH, 8, int)),
            num_modes=int(self._get(self.KEY_DOCK_MODES, 9, int)),
            energy_range=float(self._get(self.KEY_DOCK_ENERGY, 3.0, float)),
            cpu_per_job=int(self._get(self.KEY_DOCK_CPU, 4, int)),
            parallel_jobs=int(
                self._get(self.KEY_DOCK_PARALLEL, DockingDefaults().parallel_jobs, int)
            ),
        )

    def set_docking_defaults(self, d: DockingDefaults) -> None:
        self._set(self.KEY_DOCK_EXH, d.exhaustiveness)
        self._set(self.KEY_DOCK_MODES, d.num_modes)
        self._set(self.KEY_DOCK_ENERGY, d.energy_range)
        self._set(self.KEY_DOCK_CPU, d.cpu_per_job)
        self._set(self.KEY_DOCK_PARALLEL, d.parallel_jobs)

    # ------------------------------------------------------------------
    # Library defaults
    # ------------------------------------------------------------------
    def library_defaults(self) -> LibraryDefaults:
        return LibraryDefaults(
            mw_min=int(self._get(self.KEY_LIB_MW_MIN, 300, int)),
            mw_max=int(self._get(self.KEY_LIB_MW_MAX, 450, int)),
            logp_min=float(self._get(self.KEY_LIB_LOGP_MIN, -1.0, float)),
            logp_max=float(self._get(self.KEY_LIB_LOGP_MAX, 2.0, float)),
            n_compounds=int(self._get(self.KEY_LIB_N, 1000, int)),
        )

    def set_library_defaults(self, d: LibraryDefaults) -> None:
        self._set(self.KEY_LIB_MW_MIN, d.mw_min)
        self._set(self.KEY_LIB_MW_MAX, d.mw_max)
        self._set(self.KEY_LIB_LOGP_MIN, d.logp_min)
        self._set(self.KEY_LIB_LOGP_MAX, d.logp_max)
        self._set(self.KEY_LIB_N, d.n_compounds)

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def is_first_run(self) -> bool:
        return bool(self._get(self.KEY_FIRST_RUN, True, bool))

    def mark_first_run_done(self) -> None:
        self._set(self.KEY_FIRST_RUN, False)

    def show_citations_on_complete(self) -> bool:
        return bool(self._get(self.KEY_SHOW_CITATIONS, True, bool))
    def set_show_citations(self, value: bool) -> None:
        self._set(self.KEY_SHOW_CITATIONS, value)
