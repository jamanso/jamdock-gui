"""Live throughput chart for the Docking tab.

Uses :mod:`pyqtgraph` (Qt-native, no WebGL) so it renders fine on WSL
without GPU acceleration. We expose two series:

* **Rate** (orange) — instantaneous comp/min, smoothed.
* **Cumulative** (blue) — total completed jobs, secondary axis.

The widget is GUI-only — feeding it data is the caller's job:

.. code-block:: python

    chart = ThroughputChart()
    chart.reset()
    chart.add_sample(rate_per_min=12.5, cumulative=380)
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QPen
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

try:
    import pyqtgraph as pg
    _HAS_PYQTGRAPH = True
except ImportError:  # pragma: no cover - optional dependency
    pg = None  # type: ignore[assignment]
    _HAS_PYQTGRAPH = False


class ThroughputChart(QWidget):
    """Two-series rolling chart: comp/min + cumulative completions."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(160)

        self._t0: float | None = None
        self._t: list[float] = []          #: minutes since reset
        self._rate: list[float] = []
        self._cum: list[int] = []

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        if _HAS_PYQTGRAPH:
            pg.setConfigOption("background", None)  # transparent
            pg.setConfigOption("foreground", "#444")
            self._plot = pg.PlotWidget()
            self._plot.setMenuEnabled(False)
            self._plot.showGrid(x=True, y=True, alpha=0.25)
            self._plot.setLabel("bottom", "Elapsed (min)")
            self._plot.setLabel("left", "Rate (comp/min)")
            self._plot.addLegend(offset=(8, 8))

            # Rate series (orange).
            pen_rate = QPen(pg.mkColor("#f39c12"))
            pen_rate.setWidth(2)
            self._rate_curve = self._plot.plot([], [], pen=pen_rate, name="Rate")

            # Secondary axis on the right for cumulative.
            self._right_view = pg.ViewBox()
            self._plot.showAxis("right")
            self._plot.scene().addItem(self._right_view)
            self._plot.getAxis("right").linkToView(self._right_view)
            self._right_view.setXLink(self._plot)
            self._plot.getAxis("right").setLabel("Cumulative", color="#2980b9")

            pen_cum = QPen(pg.mkColor("#2980b9"))
            pen_cum.setWidth(2)
            pen_cum.setStyle(Qt.DashLine)
            self._cum_curve = pg.PlotCurveItem([], [], pen=pen_cum, name="Cumulative")
            self._right_view.addItem(self._cum_curve)
            self._plot.getViewBox().sigResized.connect(self._sync_views)
            self._sync_views()

            v.addWidget(self._plot)
        else:
            from PySide6.QtWidgets import QLabel
            placeholder = QLabel(
                "<div style='text-align:center; color:#888'>"
                "<h4>Throughput chart</h4>"
                "<p><code>pyqtgraph</code> is not installed.</p>"
                "<p><code>pip install pyqtgraph</code></p></div>"
            )
            placeholder.setAlignment(Qt.AlignCenter)
            placeholder.setStyleSheet(
                "QLabel { background: #f5f5f5; border: 1px dashed #ccc; }"
            )
            v.addWidget(placeholder)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def reset(self) -> None:
        self._t0 = None
        self._t.clear()
        self._rate.clear()
        self._cum.clear()
        if _HAS_PYQTGRAPH:
            self._rate_curve.setData([], [])
            self._cum_curve.setData([], [])

    def add_sample(self, rate_per_min: float, cumulative: int) -> None:
        """Append one data point. Time axis derived from wallclock."""
        if not _HAS_PYQTGRAPH:
            return
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        elapsed_min = (now - self._t0) / 60.0
        self._t.append(elapsed_min)
        self._rate.append(float(rate_per_min))
        self._cum.append(int(cumulative))
        self._rate_curve.setData(self._t, self._rate)
        self._cum_curve.setData(self._t, self._cum)

    # ------------------------------------------------------------------
    # Internal — keep the right axis in step with the left viewport
    # ------------------------------------------------------------------
    def _sync_views(self) -> None:
        if not _HAS_PYQTGRAPH:
            return
        self._right_view.setGeometry(self._plot.getViewBox().sceneBoundingRect())
        self._right_view.linkedViewChanged(self._plot.getViewBox(), self._right_view.XAxis)
