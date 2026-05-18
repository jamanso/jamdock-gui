"""Generic ``QProcess``-based runner for jamdock-suite bash scripts.

The runner takes care of:
* launching the script via ``bash`` (so it works regardless of the executable
  bit on bundled files),
* feeding pre-canned input to ``stdin`` (so we don't need to refactor the
  interactive ``read`` prompts),
* line-buffering stdout/stderr so the UI receives one signal per line,
* detecting **phase changes** by matching a configurable mapping of regex
  patterns to phase labels,
* parsing **progress bars** of the form ``[#### ] 45% (450/1000)``,
* a clean Stop with TERM → KILL escalation on timeout.

The class is deliberately UI-agnostic — it emits Qt signals only — so it can
be reused by other tabs (Receptor, Docking) and by tests.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

# -- defaults shared across scripts -------------------------------------
DEFAULT_PROGRESS_PATTERN = re.compile(
    r"\[[#\s]*\]\s*(?P<percent>\d+)%\s*\((?P<current>\d+)/(?P<total>\d+)\)"
)


@dataclass
class RunnerConfig:
    """Per-call configuration consumed by :meth:`ScriptRunner.start`."""

    script: Path                                #: absolute path to the bash script
    args: list[str] = field(default_factory=list)
    workdir: Path | None = None                 #: cwd for the child process
    stdin_input: bytes | None = None            #: pre-canned answers for ``read`` prompts
    env_extra: dict[str, str] = field(default_factory=dict)
    phase_patterns: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    progress_pattern: re.Pattern[str] | None = DEFAULT_PROGRESS_PATTERN
    terminate_grace_ms: int = 5000              #: TERM → KILL grace period


# -- runner -------------------------------------------------------------
class ScriptRunner(QObject):
    """Wraps a single ``QProcess`` invocation with rich output parsing.

    Signals
    -------
    line:           ``str`` — one line of stdout (newline stripped).
    err_line:       ``str`` — one line of stderr.
    phase_changed:  ``str`` — new phase label.
    progress:       ``int, int`` — ``(current, total)``; both 0 means indeterminate.
    started:        emitted right after the process is launched.
    finished:       ``int, int`` — ``(exit_code, exit_status)``.
    failed_to_start: ``str`` — error message if the process never started.
    """

    line = Signal(str)
    err_line = Signal(str)
    phase_changed = Signal(str)
    progress = Signal(int, int)
    started = Signal()
    finished = Signal(int, int)
    failed_to_start = Signal(str)

    # ------------------------------------------------------------------
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None
        self._cfg: RunnerConfig | None = None
        self._stdout_buf = bytearray()
        self._stderr_buf = bytearray()
        self._current_phase: str | None = None
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._force_kill)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def is_running(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.NotRunning

    def start(self, cfg: RunnerConfig) -> None:
        """Launch the script. Refuses if a previous run is still active."""
        if self.is_running():
            raise RuntimeError("ScriptRunner is already running.")

        bash = shutil.which("bash") or "/bin/bash"

        process = QProcess(self)
        process.setProgram(bash)
        process.setArguments([str(cfg.script), *cfg.args])
        if cfg.workdir is not None:
            process.setWorkingDirectory(str(cfg.workdir))

        env = QProcessEnvironment.systemEnvironment()
        # Force unbuffered stdout/stderr from any child Python; doesn't hurt bash.
        env.insert("PYTHONUNBUFFERED", "1")
        for k, v in cfg.env_extra.items():
            env.insert(k, v)
        process.setProcessEnvironment(env)

        process.setProcessChannelMode(QProcess.SeparateChannels)
        process.readyReadStandardOutput.connect(self._drain_stdout)
        process.readyReadStandardError.connect(self._drain_stderr)
        process.started.connect(self._on_started)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)

        self._cfg = cfg
        self._process = process
        self._stdout_buf = bytearray()
        self._stderr_buf = bytearray()
        self._current_phase = None

        process.start()
        if not process.waitForStarted(3000):
            self.failed_to_start.emit(process.errorString())
            self._process = None
            self._cfg = None

    def stop(self) -> None:
        """Politely terminate; escalate to SIGKILL after the grace period."""
        if not self._process or self._process.state() == QProcess.NotRunning:
            return
        self._process.terminate()
        grace = self._cfg.terminate_grace_ms if self._cfg else 5000
        self._kill_timer.start(grace)

    def _force_kill(self) -> None:
        if self._process and self._process.state() != QProcess.NotRunning:
            self._process.kill()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    def _on_started(self) -> None:
        # Push pre-canned input (for ``read`` prompts), then close stdin so
        # bash sees EOF and stops blocking on subsequent reads.
        if self._process and self._cfg and self._cfg.stdin_input:
            self._process.write(self._cfg.stdin_input)
        if self._process:
            self._process.closeWriteChannel()
        self.started.emit()

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        # Drain any trailing partial line.
        self._drain_stdout(final=True)
        self._drain_stderr(final=True)
        self._kill_timer.stop()
        # PySide6 ≥ 6.7 stopped auto-converting QProcess.ExitStatus to int.
        # Map to the canonical 0/1 ourselves so the signal emits cleanly on
        # any PySide6 version.
        status_int = 0 if exit_status == QProcess.ExitStatus.NormalExit else 1
        self.finished.emit(int(exit_code), status_int)
        self._process = None
        self._cfg = None

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if not self._process:
            return
        if self._process.state() == QProcess.NotRunning:
            self.failed_to_start.emit(self._process.errorString())

    # ------------------------------------------------------------------
    # Stream parsing
    # ------------------------------------------------------------------
    def _drain_stdout(self, final: bool = False) -> None:
        if not self._process:
            return
        chunk = bytes(self._process.readAllStandardOutput())
        self._stdout_buf.extend(chunk)
        for line in self._consume_lines(self._stdout_buf, final):
            self.line.emit(line)
            self._dispatch_line(line)

    def _drain_stderr(self, final: bool = False) -> None:
        if not self._process:
            return
        chunk = bytes(self._process.readAllStandardError())
        self._stderr_buf.extend(chunk)
        for line in self._consume_lines(self._stderr_buf, final):
            self.err_line.emit(line)

    @staticmethod
    def _consume_lines(buf: bytearray, final: bool) -> list[str]:
        """Pop complete lines from *buf* (in place). On ``final`` flush the rest.

        Bash progress bars use ``\\r`` to overwrite the same line many times.
        We split on either ``\\n`` or ``\\r`` so each bar update reaches the UI.
        """
        text = buf.decode("utf-8", errors="replace")
        out: list[str] = []
        # Normalise: '\r\n' → '\n', then split by either separator.
        text = text.replace("\r\n", "\n")
        # We want to keep partial line at the end (no trailing terminator).
        parts = re.split(r"[\n\r]", text)
        if final:
            tail_keep = ""
        else:
            tail_keep = parts[-1]
            parts = parts[:-1]
        for line in parts:
            if line:
                out.append(line)
        # Replace buffer contents with the leftover tail.
        buf.clear()
        buf.extend(tail_keep.encode("utf-8"))
        return out

    # ------------------------------------------------------------------
    # Phase + progress detection
    # ------------------------------------------------------------------
    def _dispatch_line(self, line: str) -> None:
        if not self._cfg:
            return

        # Phase detection.
        for label, pattern in self._cfg.phase_patterns:
            if pattern.search(line) and label != self._current_phase:
                self._current_phase = label
                self.phase_changed.emit(label)
                break

        # Progress bar detection.
        if self._cfg.progress_pattern is not None:
            m = self._cfg.progress_pattern.search(line)
            if m:
                try:
                    cur = int(m.group("current"))
                    tot = int(m.group("total"))
                except (KeyError, ValueError):
                    return
                if tot > 0:
                    self.progress.emit(cur, tot)
