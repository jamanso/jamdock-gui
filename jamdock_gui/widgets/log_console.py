"""Live, append-only log console for child-process output.

* Monospaced, dark-on-light by default (theme-aware in a future pass).
* Distinguishes stdout from stderr by colour.
* Auto-scrolls only when the user is already at the bottom — preserves the
  view if they've scrolled up to inspect older output.
* Capped at a configurable maximum number of lines to avoid unbounded growth
  during multi-hour runs.
"""
from __future__ import annotations

import html

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QWidget


class LogConsole(QPlainTextEdit):
    """Append-only live log."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        max_lines: int = 5000,
        font_family: str = "monospace",
        font_size_pt: int = 10,
    ) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(max_lines)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setPlaceholderText("Waiting for the script to start…")

        font = QFont(font_family)
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(font_size_pt)
        self.setFont(font)

        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    # ------------------------------------------------------------------
    # Public slots
    # ------------------------------------------------------------------
    @Slot(str)
    def append_stdout(self, line: str) -> None:
        self._append_line(line, html_color=None)

    @Slot(str)
    def append_stderr(self, line: str) -> None:
        # Soft red — readable on light and dark themes.
        self._append_line(line, html_color="#c0392b")

    @Slot(str)
    def append_info(self, line: str) -> None:
        # Used for our own UI messages (e.g. "▶ Starting", "■ Stopped").
        self._append_line(line, html_color="#2980b9", bold=True)

    @Slot()
    def clear_log(self) -> None:
        self.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _append_line(self, line: str, *, html_color: str | None, bold: bool = False) -> None:
        # Are we currently at the bottom?
        sb = self.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4

        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        if html_color or bold:
            style = []
            if html_color:
                style.append(f"color:{html_color}")
            if bold:
                style.append("font-weight:bold")
            payload = (
                f"<span style='{';'.join(style)}'>"
                f"{html.escape(line)}"
                f"</span>"
            )
            cursor.insertHtml(payload + "<br>")
        else:
            cursor.insertText(line + "\n")

        if at_bottom:
            sb.setValue(sb.maximum())
