"""Async wrappers around the external binaries used by the Receptor tab.

Two thin classes, both QObject:

* :class:`PrepareReceptorRunner` — runs ``pythonsh prepare_receptor4.py``.
* :class:`FpocketRunner` — runs ``fpocket -f receptor.pdbqt -o out_dir``.

Compared to :class:`~jamdock_gui.core.process_runner.ScriptRunner`, these are
single-shot binary invocations with plain line-based output, no progress
bars, no stdin payload — the simpler primitive is enough.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal


# ---------------------------------------------------------------------------
class _BinaryRunner(QObject):
    """Common machinery for running a one-shot binary asynchronously."""

    line = Signal(str)
    err_line = Signal(str)
    started = Signal()
    finished = Signal(int, int)        # (exit_code, exit_status_int)
    failed_to_start = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None
        self._stdout_buf = bytearray()
        self._stderr_buf = bytearray()
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._force_kill)

    # ------------------------------------------------------------------
    def is_running(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.NotRunning

    def stop(self, grace_ms: int = 5000) -> None:
        if not self._process or self._process.state() == QProcess.NotRunning:
            return
        self._process.terminate()
        self._kill_timer.start(grace_ms)

    def _force_kill(self) -> None:
        if self._process and self._process.state() != QProcess.NotRunning:
            self._process.kill()

    # ------------------------------------------------------------------
    def _start_process(
        self,
        program: str,
        args: list[str],
        *,
        workdir: Path | None = None,
        env_extra: dict[str, str] | None = None,
        path_prepend: list[str] | None = None,
    ) -> None:
        if self.is_running():
            raise RuntimeError("Runner is already running.")

        self._stdout_buf = bytearray()
        self._stderr_buf = bytearray()

        proc = QProcess(self)
        proc.setProgram(program)
        proc.setArguments(args)
        if workdir is not None:
            proc.setWorkingDirectory(str(workdir))
        proc.setProcessChannelMode(QProcess.SeparateChannels)

        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        if path_prepend:
            current = env.value("PATH", "")
            extras = ":".join(str(p) for p in path_prepend)
            env.insert("PATH", f"{extras}:{current}" if current else extras)
        if env_extra:
            for k, v in env_extra.items():
                env.insert(k, str(v))
        proc.setProcessEnvironment(env)

        proc.readyReadStandardOutput.connect(self._drain_stdout)
        proc.readyReadStandardError.connect(self._drain_stderr)
        proc.started.connect(self.started.emit)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)

        self._process = proc
        proc.start()
        if not proc.waitForStarted(3000):
            self.failed_to_start.emit(proc.errorString() or "Failed to start")
            self._process = None

    # ------------------------------------------------------------------
    def _drain_stdout(self, final: bool = False) -> None:
        if not self._process:
            return
        self._stdout_buf.extend(bytes(self._process.readAllStandardOutput()))
        for line in self._consume_lines(self._stdout_buf, final):
            self.line.emit(line)

    def _drain_stderr(self, final: bool = False) -> None:
        if not self._process:
            return
        self._stderr_buf.extend(bytes(self._process.readAllStandardError()))
        for line in self._consume_lines(self._stderr_buf, final):
            self.err_line.emit(line)

    @staticmethod
    def _consume_lines(buf: bytearray, final: bool) -> list[str]:
        text = buf.decode("utf-8", errors="replace").replace("\r\n", "\n")
        parts = text.split("\n")
        if final:
            tail = ""
        else:
            tail = parts[-1]
            parts = parts[:-1]
        out = [p for p in parts if p]
        buf.clear()
        buf.extend(tail.encode("utf-8"))
        return out

    # ------------------------------------------------------------------
    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        self._drain_stdout(final=True)
        self._drain_stderr(final=True)
        self._kill_timer.stop()
        # PySide6 ≥ 6.7 stopped auto-converting QProcess.ExitStatus to int.
        # Map to the canonical 0/1 ourselves so the signal emits cleanly on
        # any PySide6 version.
        status_int = 0 if exit_status == QProcess.ExitStatus.NormalExit else 1
        self.finished.emit(int(exit_code), status_int)
        self._process = None

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if not self._process:
            return
        if self._process.state() == QProcess.NotRunning:
            self.failed_to_start.emit(self._process.errorString() or "Process error")


# ---------------------------------------------------------------------------
@dataclass
class PrepareReceptorOptions:
    add_hydrogens: bool = True
    cleanup: str = "nphs_lps_waters_nonstdres"   #: -U flag value
    verbose: bool = True


class PrepareReceptorRunner(_BinaryRunner):
    """Runs MGLTools' ``prepare_receptor4.py`` via ``pythonsh``.

    Reproduces the invocation in jamreceptor::

        pythonsh prepare_receptor4.py -r <cleaned.pdb> -o <out.pdbqt> \
                 -A hydrogens -U nphs_lps_waters_nonstdres -v
    """

    def start(
        self,
        *,
        pythonsh: Path,
        prepare_receptor4: Path,
        cleaned_pdb: Path,
        output_pdbqt: Path,
        workdir: Path,
        options: PrepareReceptorOptions | None = None,
    ) -> None:
        opts = options or PrepareReceptorOptions()
        args = [
            str(prepare_receptor4),
            "-r", str(cleaned_pdb),
            "-o", str(output_pdbqt),
            "-U", opts.cleanup,
        ]
        if opts.add_hydrogens:
            args += ["-A", "hydrogens"]
        if opts.verbose:
            args.append("-v")

        # MGLTools needs its bin/ on PATH for some of its python_lib lookups.
        path_prepend: list[str] = []
        ph = Path(pythonsh)
        if ph.parent.is_dir():
            path_prepend.append(str(ph.parent))

        self._start_process(
            program=str(pythonsh),
            args=args,
            workdir=workdir,
            path_prepend=path_prepend,
        )


# ---------------------------------------------------------------------------
class FpocketRunner(_BinaryRunner):
    """Runs ``fpocket -f <pdbqt> -o <out_dir>``.

    Note that Fpocket doesn't actually accept ``-o`` everywhere — older
    versions output next to the input file as ``<base>_out/``. We support
    both behaviours: if ``out_dir`` is None we let fpocket pick the default
    location and the caller queries :attr:`expected_output_dir`.
    """

    def start(
        self,
        *,
        fpocket: Path,
        input_pdbqt: Path,
        out_dir: Path | None = None,
        workdir: Path,
    ) -> None:
        args = ["-f", str(input_pdbqt)]
        if out_dir is not None:
            args += ["-o", str(out_dir)]
        self._start_process(program=str(fpocket), args=args, workdir=workdir)


# ---------------------------------------------------------------------------
def expected_fpocket_dir(input_pdbqt: Path) -> Path:
    """Return the default ``<base>_out`` directory Fpocket creates next to its input."""
    p = Path(input_pdbqt)
    return p.with_name(p.stem + "_out")



# PDB2PQR - pH-dependent protonation of titratable residues
RESIDUE_PROTONATION_RENAMES: dict[str, str] = {
    "ASH": "ASP", "GLH": "GLU", "LYN": "LYS", "CYM": "CYS",
    "HID": "HIS", "HIE": "HIS", "HIP": "HIS",
    "TYM": "TYR", "ARN": "ARG",
    "HSD": "HIS", "HSE": "HIS", "HSP": "HIS",
    "ASPP": "ASP", "GLUP": "GLU", "LSN": "LYS",
}


def normalize_residue_names(in_pdb, out_pdb) -> dict:
    # Rename non-standard protonation residues back to standard 3-letter code
    # so MGLTools recognises them. Returns counts of each rename made.
    in_p = Path(in_pdb)
    out_p = Path(out_pdb)
    counts: dict = {}
    with in_p.open("r", encoding="utf-8", errors="replace") as fin, \
         out_p.open("w", encoding="utf-8") as fout:
        for line in fin:
            if line.startswith(("ATOM", "HETATM")) and len(line) >= 20:
                resn = line[17:20].strip()
                replacement = RESIDUE_PROTONATION_RENAMES.get(resn)
                if replacement is not None:
                    counts[resn] = counts.get(resn, 0) + 1
                    line = line[:17] + f"{replacement:<3}" + line[20:]
            fout.write(line)
    return counts


@dataclass
class Pdb2pqrOptions:
    ph: float = 7.4
    forcefield: str = "PARSE"
    use_propka: bool = True
    keep_chain: bool = True


class Pdb2pqrRunner(_BinaryRunner):
    # Run pdb2pqr30 to protonate titratable residues at a given pH.
    def start(self, *, pdb2pqr, input_pdb, output_pdb, workdir, options=None):
        opts = options or Pdb2pqrOptions()
        scratch_pqr = Path(output_pdb).with_suffix(".pqr")
        args = [
            f"--ff={opts.forcefield}",
            f"--with-ph={opts.ph:g}",
            f"--pdb-output={output_pdb}",
        ]
        if opts.use_propka:
            args.append("--titration-state-method=propka")
        if opts.keep_chain:
            args.append("--keep-chain")
        args.extend([str(input_pdb), str(scratch_pqr)])
        self._start_process(program=str(pdb2pqr), args=args, workdir=workdir)
