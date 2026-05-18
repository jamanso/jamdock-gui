"""Sidebar panel that replaces the embedded NGL viewer with a PyMOL launcher.

Why this widget exists
----------------------
The embedded NGL viewer (``widgets/viewer3d.py``) needs working WebGL,
which WSL2 doesn't provide. PyMOL has its own software OpenGL fallback
that works everywhere — so for the canonical workflow we drive PyMOL
externally.

The panel shows up where the embedded viewer used to live (right side of
the Receptor tab) and offers a single, context-aware **Open in PyMOL**
button that loads whatever's relevant for the current step:

    Step 1 (Load):              receptor PDB
    Step 2 (PDBQT):             receptor PDBQT
    Step 3 (Pockets):           receptor + Fpocket .pml (numbered spheres)
    Step 4 (Grid box):          receptor + .pml + grid_box.py wireframe

The widget exposes the **same API as Viewer3D** (``load_pdb_file``,
``set_pocket_spheres``, ``set_grid_box``, ``clear``) so the call sites in
``receptor_tab.py`` don't have to change. The methods just record state
that the launch button consumes when clicked.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from jamdock_gui.core.pymol_launcher import (
    find_pymol,
)


class PymolPanel(QWidget):
    """Drop-in replacement for ``Viewer3D`` that drives PyMOL externally.

    The public API mirrors ``Viewer3D``:

    * :meth:`load_pdb_file` — record current structure path.
    * :meth:`load_pdb` — string variant; we ignore the body and only mark state.
    * :meth:`set_pocket_spheres` — record that pockets are available.
    * :meth:`set_grid_box` — record current grid box parameters.
    * :meth:`clear` — drop everything.
    """

    pymol_launched = Signal()  # emitted whenever PyMOL is spawned

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Tracked context — the launch button uses these.
        self._current_pdb: Path | None = None
        self._fpocket_pml: Path | None = None
        self._grid_box_py: Path | None = None
        # ``set_pocket_spheres([])`` clears _pocket_count; positive number
        # signals the user to use the "preview pockets" workflow.
        self._pocket_count: int = 0

        self._build()
        self._refresh_status()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel("<h3>PyMOL viewer</h3>")
        title.setAlignment(Qt.AlignLeft)
        layout.addWidget(title)

        intro = QLabel(
            "<p>This GUI uses <b>PyMOL</b> as its 3D viewer. Each step has "
            "its own <b>View in PyMOL</b> button on the left to open the "
            "relevant files in a separate window:</p>"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        steps = QLabel(
            "<ul style='margin-left:-20px'>"
            "<li><b>Step 1</b> — input PDB</li>"
            "<li><b>Step 2</b> — receptor PDBQT</li>"
            "<li><b>Step 3</b> — receptor + numbered pockets (Fpocket)</li>"
            "<li><b>Step 4</b> — receptor + pockets + grid-box wireframe</li>"
            "</ul>"
        )
        steps.setWordWrap(True)
        steps.setTextFormat(Qt.RichText)
        layout.addWidget(steps)

        # Status & current-context labels.
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("QLabel { color: #555; }")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)

        self.lbl_context = QLabel("")
        self.lbl_context.setWordWrap(True)
        self.lbl_context.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_context.setStyleSheet(
            "QLabel { background:#f8f8f8; border:1px solid #e0e0e0;"
            " padding:8px; border-radius:3px; font-family: monospace;"
            " font-size: 9pt; }"
        )
        layout.addWidget(self.lbl_context)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep)

        tip = QLabel(
            "<p style='color:#888; font-size:small'>"
            "<i>Tip: in Step 3 use the button to preview pockets in PyMOL "
            "<b>before</b> ticking which ones to use. Each pocket is "
            "labelled <code>Pocket N</code> and coloured differently in "
            "the Fpocket-generated session.</i></p>"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        layout.addStretch(1)

    # ------------------------------------------------------------------
    # Public API (matches Viewer3D so receptor_tab.py call sites don't change)
    # ------------------------------------------------------------------
    def load_pdb_file(self, path: Path | str, *, format: str | None = None) -> None:
        self._current_pdb = Path(path) if path else None
        self._refresh_status()

    def load_pdb(self, pdb_text: str, *, format: str = "pdb") -> None:
        # We don't carry an in-memory PDB — PyMOL needs a file. The caller
        # can pair this with load_pdb_file when a real path becomes available.
        self._refresh_status()

    def set_chain_selection(self, chains: Iterable[str] | None) -> None:
        # Chain selection is an embedded-viewer cosmetic; PyMOL handles
        # selection via its own UI, so this is a no-op here.
        pass

    def set_pocket_spheres(self, pockets: Iterable[dict] | None) -> None:
        self._pocket_count = len(list(pockets)) if pockets else 0
        self._refresh_status()

    def set_grid_box(
        self,
        center: tuple[float, float, float] | None,
        size: tuple[float, float, float] | None,
    ) -> None:
        # The actual file is written by Step 4's "Save grid_box.py" button;
        # we rebuild the path here from the work-dir convention.
        # The owner widget will call ``set_grid_box_py(path)`` if it has one.
        if center is None or size is None:
            self._grid_box_py = None
        self._refresh_status()

    def clear(self) -> None:
        self._current_pdb = None
        self._fpocket_pml = None
        self._grid_box_py = None
        self._pocket_count = 0
        self._refresh_status()

    # ------------------------------------------------------------------
    # Extension points used directly by ReceptorTab
    # ------------------------------------------------------------------
    def set_fpocket_pml(self, path: Path | None) -> None:
        """Tell the panel where Fpocket's session script lives."""
        self._fpocket_pml = Path(path) if path else None
        self._refresh_status()

    def set_grid_box_py(self, path: Path | None) -> None:
        """Tell the panel where ``grid_box.py`` lives on disk."""
        self._grid_box_py = Path(path) if path else None
        self._refresh_status()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _refresh_status(self) -> None:
        pymol = find_pymol()
        have_files = self._current_pdb is not None and self._current_pdb.is_file()
        # Auto-detect companions on every refresh so the status panel reflects
        # the disk truth even if Step 3 / Step 4 haven't fired in this session.
        if have_files:
            stem = self._current_pdb.stem
            parent = self._current_pdb.parent
            if not (self._fpocket_pml and self._fpocket_pml.is_file()):
                cand = parent / f"{stem}_out" / f"{stem}.pml"
                if cand.is_file():
                    self._fpocket_pml = cand
            if not (self._grid_box_py and self._grid_box_py.is_file()):
                cand = parent / "grid_box.py"
                if cand.is_file():
                    self._grid_box_py = cand

        # Status line
        if not pymol:
            self.lbl_status.setText(
                "PyMOL not found on PATH. Install it (e.g. "
                "sudo apt install pymol) and relaunch the app."
            )
        elif not have_files:
            self.lbl_status.setText(
                "PyMOL detected. Use a step's View in PyMOL button on the left "
                "to launch it with the relevant files."
            )
        else:
            self.lbl_status.setText(
                "PyMOL detected. Use a step's View in PyMOL button on the left."
            )

        # Context (what would be loaded if you launch from a step button)
        ctx_lines: list[str] = []
        if self._current_pdb:
            ctx_lines.append(f"structure : {self._current_pdb.name}")
        if self._fpocket_pml and self._fpocket_pml.is_file():
            n = f" ({self._pocket_count} pockets)" if self._pocket_count else ""
            ctx_lines.append(f"pockets   : {self._fpocket_pml.name}{n}")
        if self._grid_box_py and self._grid_box_py.is_file():
            ctx_lines.append(f"grid box  : {self._grid_box_py.name}")
        if ctx_lines:
            self.lbl_context.setText("\n".join(ctx_lines))
        else:
            self.lbl_context.setText("(nothing loaded yet)")
