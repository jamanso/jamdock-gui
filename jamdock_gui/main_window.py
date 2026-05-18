"""Top-level window: tab container, menus, status bar, working-directory selector."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from jamdock_gui import APP_NAME, __version__
from jamdock_gui.deps import probe_all
from jamdock_gui.settings import Settings
from jamdock_gui.tabs.docking_tab import DockingTab
from jamdock_gui.tabs.library_tab import LibraryTab
from jamdock_gui.tabs.receptor_tab import ReceptorTab
from jamdock_gui.tabs.results_tab import ResultsTab


class MainWindow(QMainWindow):
    """Main application window with the four tabs of the pipeline."""

    workdir_changed = Signal(Path)

    def __init__(self) -> None:
        super().__init__()
        self.settings = Settings()
        self._workdir: Path | None = self.settings.last_workdir()

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1280, 820)

        self._build_central()
        self._build_menus()
        self._build_toolbar()
        self._build_statusbar()
        self._wire_signals()
        self._refresh_dep_status()

    # ------------------------------------------------------------------
    def _build_central(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._project_bar = self._build_project_bar()
        layout.addWidget(self._project_bar)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabPosition(QTabWidget.North)

        self.library_tab = LibraryTab(self.settings, self)
        self.receptor_tab = ReceptorTab(self.settings, self)
        self.docking_tab = DockingTab(self.settings, self)
        self.results_tab = ResultsTab(self.settings, self)

        self.tabs.addTab(self.library_tab, "1. Library")
        self.tabs.addTab(self.receptor_tab, "2. Receptor")
        self.tabs.addTab(self.docking_tab, "3. Docking")
        self.tabs.addTab(self.results_tab, "4. Results")

        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

    def _build_project_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("projectBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(12, 8, 12, 8)
        h.addWidget(QLabel("<b>Working directory:</b>"))
        self._workdir_label = QLabel(str(self._workdir or "(not set)"))
        self._workdir_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        h.addWidget(self._workdir_label, 1)
        choose_btn = QPushButton("Choose...")
        choose_btn.clicked.connect(self._choose_workdir)
        h.addWidget(choose_btn)
        return bar

    def _build_menus(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")
        a_choose = QAction("&Open working directory...", self)
        a_choose.setShortcut(QKeySequence.Open)
        a_choose.triggered.connect(self._choose_workdir)
        file_menu.addAction(a_choose)
        file_menu.addSeparator()
        a_quit = QAction("&Quit", self)
        a_quit.setShortcut(QKeySequence.Quit)
        a_quit.triggered.connect(self.close)
        file_menu.addAction(a_quit)

        tools_menu = menu.addMenu("&Tools")
        a_deps = QAction("Check &dependencies...", self)
        a_deps.triggered.connect(self._show_dep_dialog)
        tools_menu.addAction(a_deps)

        help_menu = menu.addMenu("&Help")
        a_about = QAction("&About jamdock-gui", self)
        a_about.triggered.connect(self._show_about)
        help_menu.addAction(a_about)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        self.addToolBar(tb)

    def _build_statusbar(self) -> None:
        sb = QStatusBar(self)
        self.setStatusBar(sb)
        self._status_deps_label = QLabel("Checking dependencies...")
        sb.addPermanentWidget(self._status_deps_label)

    def _wire_signals(self) -> None:
        self.workdir_changed.connect(self.library_tab.on_workdir_changed)
        self.workdir_changed.connect(self.receptor_tab.on_workdir_changed)
        self.workdir_changed.connect(self.docking_tab.on_workdir_changed)
        self.workdir_changed.connect(self.results_tab.on_workdir_changed)
        self.docking_tab.pool_started.connect(self.results_tab.attach_pool)
        self.docking_tab.pool_stopped.connect(self.results_tab.detach_pool)

    # ------------------------------------------------------------------
    @property
    def workdir(self) -> Path | None:
        return self._workdir

    def _choose_workdir(self) -> None:
        start = str(self._workdir or Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select working directory", start)
        if chosen:
            self._set_workdir(Path(chosen))

    def _set_workdir(self, path: Path) -> None:
        self._workdir = path
        self.settings.set_last_workdir(path)
        self._workdir_label.setText(str(path))
        self.workdir_changed.emit(path)

    # ------------------------------------------------------------------
    def _refresh_dep_status(self) -> None:
        statuses = probe_all(self.settings)
        missing = [n for n, s in statuses.items() if not s.found]
        if not missing:
            self._status_deps_label.setText("All dependencies detected")
        else:
            self._status_deps_label.setText("Missing: " + ", ".join(missing))

    def _show_dep_dialog(self) -> None:
        statuses = probe_all(self.settings)
        lines = []
        for name, st in statuses.items():
            mark = "OK" if st.found else "missing"
            path = st.path or "(not found)"
            ver = "  [" + st.version + "]" if st.version else ""
            lines.append(f"[{mark}] {name:24s} {path}{ver}")
            if not st.found and st.notes:
                lines.append("    " + st.notes)
        QMessageBox.information(self, "Dependency status", "\n".join(lines))

    def _show_about(self) -> None:
        msg = f"jamdock-gui {__version__}\nGUI for jamdock-suite\nLicense: CC BY-NC 4.0"
        QMessageBox.about(self, f"About {APP_NAME}", msg)
