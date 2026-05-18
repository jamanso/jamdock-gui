"""Per-working-directory persistent state for docking runs.

Writes :data:`STATE_FILENAME` (``.jamdock_state.json``) inside the user's
working directory. The file holds:

* ``schema_version`` — bump on incompatible changes (used by ``load_state``
  to fall back gracefully).
* ``run_started_at`` — ISO-8601 UTC.
* ``config`` — receptor, grid, library, all qvina parameters (so we can
  warn the user if they try to resume with different parameters).
* ``jobs`` — list of dicts: ligand path (relative to workdir), status,
  best score, duration_s, error.

The file is rewritten atomically (write to a tmp file then ``os.replace``)
so a crash mid-write never corrupts the run state.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

STATE_FILENAME = ".jamdock_state.json"
SCHEMA_VERSION = 1


@dataclass
class JobState:
    """Per-ligand state row persisted to disk."""

    ligand: str                       #: relative path inside the workdir
    status: str = "queued"            #: queued | running | done | failed | skipped
    best_score: float | None = None
    n_modes: int = 0
    duration_s: float | None = None
    error: str | None = None


@dataclass
class RunState:
    """Whole-run snapshot persisted to ``.jamdock_state.json``."""

    receptor: str | None = None
    grid_conf: str | None = None
    ligand_dir: str | None = None
    exhaustiveness: int = 8
    num_modes: int = 9
    energy_range: float = 3.0
    cpu_per_job: int = 4
    parallel_jobs: int = 1
    run_started_at: str | None = None
    schema_version: int = SCHEMA_VERSION
    jobs: list[JobState] = field(default_factory=list)

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_started_at": self.run_started_at,
            "config": {
                "receptor": self.receptor,
                "grid_conf": self.grid_conf,
                "ligand_dir": self.ligand_dir,
                "exhaustiveness": self.exhaustiveness,
                "num_modes": self.num_modes,
                "energy_range": self.energy_range,
                "cpu_per_job": self.cpu_per_job,
                "parallel_jobs": self.parallel_jobs,
            },
            "jobs": [
                {
                    "ligand": j.ligand,
                    "status": j.status,
                    "best_score": j.best_score,
                    "n_modes": j.n_modes,
                    "duration_s": j.duration_s,
                    "error": j.error,
                }
                for j in self.jobs
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RunState":
        cfg = data.get("config", {}) or {}
        jobs_raw = data.get("jobs", []) or []
        return cls(
            receptor=cfg.get("receptor"),
            grid_conf=cfg.get("grid_conf"),
            ligand_dir=cfg.get("ligand_dir"),
            exhaustiveness=int(cfg.get("exhaustiveness", 8)),
            num_modes=int(cfg.get("num_modes", 9)),
            energy_range=float(cfg.get("energy_range", 3.0)),
            cpu_per_job=int(cfg.get("cpu_per_job", 4)),
            parallel_jobs=int(cfg.get("parallel_jobs", 1)),
            run_started_at=data.get("run_started_at"),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            jobs=[
                JobState(
                    ligand=str(j.get("ligand", "")),
                    status=str(j.get("status", "queued")),
                    best_score=(
                        float(j["best_score"])
                        if j.get("best_score") is not None else None
                    ),
                    n_modes=int(j.get("n_modes", 0)),
                    duration_s=(
                        float(j["duration_s"])
                        if j.get("duration_s") is not None else None
                    ),
                    error=j.get("error"),
                )
                for j in jobs_raw
            ],
        )

    # ------------------------------------------------------------------
    def stamp_started_now(self) -> None:
        self.run_started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    @property
    def n_done(self) -> int:
        return sum(1 for j in self.jobs if j.status == "done")

    @property
    def n_failed(self) -> int:
        return sum(1 for j in self.jobs if j.status == "failed")

    @property
    def n_pending(self) -> int:
        return sum(1 for j in self.jobs if j.status in ("queued", "running"))


# ----------------------------------------------------------------------
# Disk I/O
# ----------------------------------------------------------------------
def state_path(workdir: Path | str) -> Path:
    return Path(workdir) / STATE_FILENAME


def save_state(workdir: Path | str, state: RunState) -> Path:
    """Atomically write *state* to ``<workdir>/.jamdock_state.json``."""
    target = state_path(workdir)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = json.dumps(state.to_dict(), indent=2)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)
    return target


def load_state(workdir: Path | str) -> RunState | None:
    """Read the state file. Returns ``None`` if it doesn't exist or is unreadable."""
    target = state_path(workdir)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return RunState.from_dict(data)
    except (TypeError, ValueError, KeyError):
        return None


def delete_state(workdir: Path | str) -> bool:
    """Remove the state file. Returns True if a file was deleted."""
    target = state_path(workdir)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
