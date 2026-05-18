"""Common base class for all tabs."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QWidget

if TYPE_CHECKING:
    from jamdock_gui.settings import Settings


class BaseTab(QWidget):
    """Shared API for tabs: settings, working directory, lifecycle hooks."""

    def __init__(self, settings: "Settings", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._workdir: Path | None = settings.last_workdir()

    # ------------------------------------------------------------------
    # Working directory
    # ------------------------------------------------------------------
    @property
    def workdir(self) -> Path | None:
        return self._workdir

    @Slot(Path)
    def on_workdir_changed(self, path: Path) -> None:
        self._workdir = path
        self.refresh_from_workdir()

    def refresh_from_workdir(self) -> None:
        """Override in subclasses to react to a new working directory."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def require_workdir(self) -> Path | None:
        """Return current workdir or ``None`` and let the caller decide."""
        return self._workdir
