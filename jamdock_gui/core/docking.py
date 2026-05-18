"""Concurrent docking orchestrator — replaces the bash loop in ``jamqvina``.

Architecture
------------
The user picks N parallel jobs × C cpu/job (capped to ``os.cpu_count()``).
We maintain a queue of :class:`DockingJob` objects; each job is one ligand
to dock. A pool of N concurrent ``QProcess`` workers drains the queue:

    queue ── pop ──> worker(QProcess) ──finished──> next pop
                          │
                          ├── parses log on completion
                          ├── persists state.json after each finish
                          └── emits signals for the GUI

All public methods are thread-safe insofar as Qt's event loop is — they're
expected to be called from the GUI thread.

Resume semantics
----------------
On :meth:`DockingPool.start` we:

1. Detect existing ``<output_dir>/<ligand>_docking.pdbqt`` files. If a file
   exists and is non-empty, the ligand is marked ``done`` (we keep its log
   and parse the score from it so the table fills out).
2. Everything else is enqueued.
3. The GUI receives a ``resume_summary`` signal with ``(n_already_done,
   n_to_do)`` so it can show a one-line banner.
"""
from __future__ import annotations

import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal, Slot

from jamdock_gui.core.qvina_log import parse_qvina_log
from jamdock_gui.core.state import JobState, RunState, load_state, save_state


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclass
class DockingConfig:
    """Run-wide parameters; the per-ligand bits live in :class:`DockingJob`."""

    qvina_path: Path                    #: ``qvina02`` binary
    receptor: Path                      #: prepared PDBQT receptor
    grid_conf: Path                     #: ``grid.conf`` written by jamreceptor
    output_dir: Path                    #: where ``<lig>_docking.pdbqt`` lands
    workdir: Path                       #: parent of output_dir; cwd for QProcess
    exhaustiveness: int = 8
    num_modes: int = 9
    energy_range: float = 3.0
    cpu_per_job: int = 4
    parallel_jobs: int = 1


@dataclass
class DockingJob:
    """One ligand. Mutated in place as the run progresses."""

    ligand: Path                        #: input PDBQT
    out_pdbqt: Path                     #: target output (Vina poses)
    log_path: Path                      #: target stdout/stderr log
    status: str = "queued"              #: queued | running | done | failed | skipped
    best_score: float | None = None
    n_modes: int = 0
    started_at: float | None = None     #: wallclock seconds (time.monotonic)
    duration_s: float | None = None
    error: str | None = None

    @property
    def name(self) -> str:
        return self.ligand.stem


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------
class DockingPool(QObject):
    """Drives a queue of :class:`DockingJob` against ``qvina02`` via QProcess.

    Signals
    -------
    job_started:     ``int`` — index in self.jobs of the ligand that just started
    job_finished:    ``int`` — same, after a worker writes its output and parses
    job_failed:      ``int, str`` — index, error message
    log_line:        ``str`` — combined stdout line from any worker (rare; mostly
                     used to surface the final qvina banner)
    err_line:        ``str`` — stderr line; surfaces qvina warnings
    progress:        ``int, int`` — (done_or_failed, total)
    throughput:      ``float`` — instantaneous comp/min (smoothed)
    eta_changed:     ``int`` — ETA in seconds, or ``-1`` while estimating
    all_finished:    emitted when the queue is empty AND no worker is running
    paused_changed:  ``bool``
    resume_summary:  ``int, int`` — (n_already_done, n_to_do) at start time
    """

    job_started = Signal(int)
    job_finished = Signal(int)
    job_failed = Signal(int, str)
    log_line = Signal(str)
    err_line = Signal(str)
    progress = Signal(int, int)
    throughput = Signal(float)
    eta_changed = Signal(int)
    all_finished = Signal()
    paused_changed = Signal(bool)
    resume_summary = Signal(int, int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cfg: DockingConfig | None = None
        self.jobs: list[DockingJob] = []
        self._queue: deque[int] = deque()      #: indices into self.jobs
        # idx -> QProcess currently running it
        self._workers: dict[int, QProcess] = {}
        self._paused: bool = False
        self._stopping: bool = False
        # Smoothed throughput accounting.
        self._t_run_start: float | None = None
        # Throttle state.json writes — on huge libraries (~500k jobs) the
        # full JSON serialisation gets very expensive if we save after
        # every single ligand finishes. Save at most every N seconds.
        self._last_state_save_at: float = 0.0
        self._state_save_interval_s: float = 5.0
        self._completed_ts: deque[float] = deque(maxlen=50)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self, jobs: list[DockingJob], cfg: DockingConfig) -> None:
        """Configure the run, populate the queue, spin up the workers."""
        if self.is_running():
            raise RuntimeError("DockingPool is already running.")

        self._cfg = cfg
        self.jobs = list(jobs)
        self._queue.clear()
        self._workers.clear()
        self._paused = False
        self._stopping = False
        self._completed_ts.clear()

        # Detect already-done ligands: <out_pdbqt> exists and non-empty.
        n_already_done = 0
        for idx, job in enumerate(self.jobs):
            try:
                if job.out_pdbqt.is_file() and job.out_pdbqt.stat().st_size > 0:
                    self._mark_done_from_existing(job)
                    n_already_done += 1
                    continue
            except OSError:
                pass
            self._queue.append(idx)

        n_to_do = len(self._queue)
        self.resume_summary.emit(n_already_done, n_to_do)
        self.progress.emit(n_already_done, len(self.jobs))
        self._save_state(force=True)

        if n_to_do == 0:
            self.all_finished.emit()
            return

        self._t_run_start = time.monotonic()
        self._fill_workers()

    def pause(self) -> None:
        """Stop launching new workers — let in-flight jobs finish naturally."""
        if self._paused:
            return
        self._paused = True
        self.paused_changed.emit(True)

    def resume(self) -> None:
        """Resume launching workers from the queue."""
        if not self._paused:
            return
        self._paused = False
        self.paused_changed.emit(False)
        self._fill_workers()

    def stop(self) -> None:
        """Kill in-flight workers and drain the queue."""
        if not self.is_running():
            return
        self._stopping = True
        self._queue.clear()
        for proc in list(self._workers.values()):
            if proc.state() != QProcess.NotRunning:
                proc.kill()
        # `_on_worker_finished` cleans up self._workers and may emit all_finished.

    def is_running(self) -> bool:
        return bool(self._workers) or bool(self._queue)

    @property
    def total(self) -> int:
        return len(self.jobs)

    @property
    def done(self) -> int:
        return sum(1 for j in self.jobs if j.status in ("done", "failed", "skipped"))

    # ------------------------------------------------------------------
    # Internals — worker lifecycle
    # ------------------------------------------------------------------
    def _fill_workers(self) -> None:
        """Spin up workers until the pool is full or the queue is empty."""
        if not self._cfg or self._paused or self._stopping:
            return
        capacity = max(1, int(self._cfg.parallel_jobs)) - len(self._workers)
        for _ in range(capacity):
            if not self._queue:
                return
            idx = self._queue.popleft()
            self._launch(idx)

    def _launch(self, idx: int) -> None:
        if not self._cfg:
            return
        job = self.jobs[idx]
        cfg = self._cfg

        try:
            cfg.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._mark_failed(idx, f"could not create output dir: {exc}")
            return

        program = str(cfg.qvina_path) if shutil.which(str(cfg.qvina_path)) or Path(cfg.qvina_path).is_file() else "qvina02"
        args = [
            "--config", str(cfg.grid_conf),
            "--receptor", str(cfg.receptor),
            "--ligand", str(job.ligand),
            "--exhaustiveness", str(cfg.exhaustiveness),
            "--num_modes", str(cfg.num_modes),
            "--energy_range", str(cfg.energy_range),
            "--cpu", str(cfg.cpu_per_job),
            "--out", str(job.out_pdbqt),
        ]

        proc = QProcess(self)
        proc.setProgram(program)
        proc.setArguments(args)
        proc.setWorkingDirectory(str(cfg.workdir))
        proc.setProcessChannelMode(QProcess.MergedChannels)

        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        proc.setProcessEnvironment(env)

        # Bind ``idx`` into the slot via default-arg trick.
        proc.readyReadStandardOutput.connect(
            lambda i=idx: self._drain_worker_output(i)
        )
        proc.finished.connect(
            lambda code, status, i=idx: self._on_worker_finished(i, code, status)
        )
        proc.errorOccurred.connect(
            lambda err, i=idx: self._on_worker_error(i, err)
        )

        # Open the per-ligand log file ourselves: qvina prints to stdout, and
        # we want a persistent ``.log`` next to the .pdbqt regardless of pipes.
        # We append a small header so users can tell what produced the log.
        try:
            with job.log_path.open("w", encoding="utf-8") as fh:
                fh.write(
                    f"# jamdock-gui — qvina02 invocation\n"
                    f"# program: {program}\n"
                    f"# args:    {' '.join(args)}\n"
                    f"# started: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
                    f"# ----------------------------------------------\n"
                )
        except OSError:
            # Non-fatal: parser will flag empty logs later.
            pass

        job.status = "running"
        job.started_at = time.monotonic()
        job.error = None
        self._workers[idx] = proc

        proc.start()
        # `started` isn't strictly necessary; we forward our own job_started.
        self.job_started.emit(idx)

    @Slot()
    def _drain_worker_output(self, idx: int) -> None:
        proc = self._workers.get(idx)
        if not proc:
            return
        try:
            chunk = bytes(proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        except Exception:
            return
        if not chunk:
            return
        # Append to per-ligand log on disk.
        job = self.jobs[idx]
        try:
            with job.log_path.open("a", encoding="utf-8") as fh:
                fh.write(chunk)
        except OSError:
            pass
        # Forward only "interesting" lines to the GUI to avoid log spam: qvina
        # prints a lot of progress dots that aren't useful at scale.
        for raw in chunk.splitlines():
            line = raw.rstrip()
            if not line:
                continue
            stripped = line.strip()
            if stripped.startswith(("|", "-", "*")):
                continue   # the dot-progress and table separators
            if stripped.startswith(("Detected", "Reading", "Writing")):
                continue
            self.log_line.emit(line[:240])

    @Slot()
    def _on_worker_finished(
        self, idx: int, exit_code: int, exit_status: QProcess.ExitStatus
    ) -> None:
        proc = self._workers.pop(idx, None)
        if proc is not None:
            proc.deleteLater()

        job = self.jobs[idx]
        # PySide6 ≥ 6.7 stopped auto-converting the enum.
        crashed = exit_status != QProcess.ExitStatus.NormalExit

        if self._stopping:
            # User asked to abort; don't try to parse or count it.
            job.status = "skipped"
            job.error = "stopped by user"
            self._after_job(idx)
            return

        if exit_code != 0 or crashed:
            self._mark_failed(idx, f"qvina exit {exit_code} ({'crash' if crashed else 'error'})")
            return

        if not job.out_pdbqt.is_file() or job.out_pdbqt.stat().st_size == 0:
            self._mark_failed(idx, "no output produced")
            return

        # Parse the log for the best score.
        log = parse_qvina_log(job.log_path)
        if log.is_empty:
            self._mark_failed(idx, log.error_text or "empty log")
            return

        job.best_score = log.best_score
        job.n_modes = log.n_modes
        if job.started_at is not None:
            job.duration_s = time.monotonic() - job.started_at
        job.status = "done"
        job.error = None

        self.job_finished.emit(idx)
        self._after_job(idx)

    @Slot()
    def _on_worker_error(self, idx: int, error: QProcess.ProcessError) -> None:
        # Surface FailedToStart specially; other errors are handled by `finished`.
        if error == QProcess.FailedToStart:
            self._mark_failed(idx, "failed to start qvina02 (binary missing or not executable)")

    # ------------------------------------------------------------------
    # Internals — bookkeeping
    # ------------------------------------------------------------------
    def _mark_failed(self, idx: int, msg: str) -> None:
        proc = self._workers.pop(idx, None)
        if proc is not None and proc.state() != QProcess.NotRunning:
            proc.kill()
        job = self.jobs[idx]
        job.status = "failed"
        job.error = msg
        if job.started_at is not None and job.duration_s is None:
            job.duration_s = time.monotonic() - job.started_at
        self.job_failed.emit(idx, msg)
        self._after_job(idx)

    def _mark_done_from_existing(self, job: DockingJob) -> None:
        """Treat a prior ``<lig>_docking.pdbqt`` as a finished job (for resume)."""
        log = parse_qvina_log(job.log_path)
        job.best_score = log.best_score
        job.n_modes = log.n_modes
        job.duration_s = None
        job.status = "done"
        job.error = log.error_text if log.is_empty else None

    def _after_job(self, idx: int) -> None:
        """Persist + recompute throughput/ETA + maybe pull next + emit progress."""
        # Throughput accounting.
        now = time.monotonic()
        if self.jobs[idx].status in ("done", "failed", "skipped"):
            self._completed_ts.append(now)

        self._save_state()

        # Re-fill the pool unless paused/stopping.
        self._fill_workers()

        # Emit progress + ETA + throughput.
        done = self.done
        total = self.total
        self.progress.emit(done, total)

        rate_per_min, eta_seconds = self._compute_rate_and_eta()
        if rate_per_min is not None:
            self.throughput.emit(rate_per_min)
        if eta_seconds is not None:
            self.eta_changed.emit(int(eta_seconds))

        # Are we fully drained?
        if not self._workers and not self._queue:
            # Final flush so the on-disk state matches reality on completion.
            self._save_state(force=True)
            self.all_finished.emit()
            self._stopping = False

    def _compute_rate_and_eta(self) -> tuple[float | None, float | None]:
        if len(self._completed_ts) < 2 or self._t_run_start is None:
            return None, None
        # Smoothed rate: completions per minute over the recent window.
        window = self._completed_ts
        elapsed = window[-1] - window[0]
        if elapsed <= 0:
            return None, None
        rate_per_sec = (len(window) - 1) / elapsed
        rate_per_min = rate_per_sec * 60.0

        remaining = max(0, self.total - self.done)
        if rate_per_sec > 0 and remaining > 0:
            eta = remaining / rate_per_sec
        elif remaining == 0:
            eta = 0.0
        else:
            eta = None
        return rate_per_min, eta

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------
    def _save_state(self, *, force: bool = False) -> None:
        """Persist state.json. Throttled to at most once per *interval* seconds
        unless *force* is True (used at start / stop / all_finished).

        Building the JSON requires touching every job — on a 500k-ligand run
        that's expensive enough that doing it after every single finish was
        the bottleneck that prevented Tab 3 from actually starting.
        """
        if not self._cfg:
            return
        now = time.monotonic()
        if not force and (now - self._last_state_save_at) < self._state_save_interval_s:
            return
        self._last_state_save_at = now
        state = self._build_state()
        try:
            save_state(self._cfg.workdir, state)
        except OSError:
            pass

    def _build_state(self) -> RunState:
        """Build a **skinny** state snapshot: run config + summary only.

        The per-job list is intentionally *not* populated. On huge libraries
        (~500k ligands) writing the full list after every job-finished
        froze the GUI for seconds each cycle. The per-job scores live in
        the qvina `.log` files anyway — Tab 4 reads them lazily on
        demand, and resume re-detects done jobs by scanning
        ``docking_results/*.pdbqt`` on disk. So the per-job state is
        redundant.
        """
        cfg = self._cfg
        assert cfg is not None
        state = RunState(
            receptor=str(cfg.receptor),
            grid_conf=str(cfg.grid_conf),
            ligand_dir=str(cfg.output_dir.parent / cfg.output_dir.name),
            exhaustiveness=cfg.exhaustiveness,
            num_modes=cfg.num_modes,
            energy_range=cfg.energy_range,
            cpu_per_job=cfg.cpu_per_job,
            parallel_jobs=cfg.parallel_jobs,
        )
        if state.run_started_at is None:
            state.stamp_started_now()
        # state.jobs deliberately left empty — see docstring.
        return state


# ---------------------------------------------------------------------------
# Convenience: build the job list from a ligand directory
# ---------------------------------------------------------------------------
def discover_jobs(
    ligand_dir: Path,
    output_dir: Path,
) -> list[DockingJob]:
    """Return one :class:`DockingJob` per ``*.pdbqt`` ligand in *ligand_dir*."""
    ligand_dir = Path(ligand_dir)
    output_dir = Path(output_dir)
    jobs: list[DockingJob] = []
    for lig in sorted(ligand_dir.glob("*.pdbqt")):
        out = output_dir / f"{lig.stem}_docking.pdbqt"
        log = output_dir / f"{lig.stem}_docking.pdbqt.log"
        jobs.append(DockingJob(ligand=lig, out_pdbqt=out, log_path=log))
    return jobs


def restore_state_into_jobs(state, jobs: list[DockingJob]) -> None:
    """Apply previously-saved status fields to *jobs* in place (resume)."""
    if not state:
        return
    by_name = {Path(j.ligand).name: j for j in state.jobs}
    for job in jobs:
        prev = by_name.get(job.ligand.name)
        if not prev:
            continue
        if prev.best_score is not None:
            job.best_score = prev.best_score
        if prev.n_modes:
            job.n_modes = prev.n_modes
        if prev.error:
            job.error = prev.error
