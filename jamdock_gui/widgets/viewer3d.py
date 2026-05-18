"""Embedded 3D molecular viewer based on NGL Viewer.

How it works
------------
We host a :class:`QWebEngineView` showing a tiny HTML page that loads
NGL.js from a CDN (a future iteration will ship the bundle as a package
resource for offline use). The HTML page defines a small JavaScript
bridge that we call from Python via
:meth:`QWebEngineView.page().runJavaScript`.

Public API (synchronous from the caller's POV — commands are queued until
the WebEngine page has finished loading NGL):

* :meth:`Viewer3D.load_pdb` — load a structure from a string blob.
* :meth:`Viewer3D.set_chain_selection` — show only the requested chains.
* :meth:`Viewer3D.set_pocket_spheres` — overlay alpha-sphere clusters.
* :meth:`Viewer3D.set_grid_box` — overlay a wireframe box for AutoDock Vina.
* :meth:`Viewer3D.clear` — drop everything.

Fallback
--------
If ``PySide6.QtWebEngineWidgets`` isn't installed, :class:`Viewer3D`
instantiates as a stub :class:`QLabel` that explains how to install it.
The rest of the GUI keeps working — only the 3D preview is missing.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

log = logging.getLogger(__name__)

# Try to import the optional WebEngine dependency. If it fails we provide a
# graceful stub so the rest of the GUI can still run.
try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEnginePage  # noqa: F401
    _HAS_WEBENGINE = True
except ImportError:  # pragma: no cover - environment-dependent
    QWebEngineView = None  # type: ignore[assignment]
    _HAS_WEBENGINE = False


# ---------------------------------------------------------------------------
# HTML host page
# ---------------------------------------------------------------------------
# NGL is loaded from a CDN. We try multiple mirrors in order so the viewer
# still works if one is down or blocked. We use ngl@2.0.0-dev.39 (UMD build,
# global ``window.NGL``) — the version used by NGL's own canonical demos and
# the nglview Jupyter widget. Newer 2.3.x is ESM-only on cdnjs and breaks
# embedded use ("NGL.Stage is not a constructor").
_NGL_CDNS = [
    "https://unpkg.com/ngl@2.0.0-dev.39/dist/ngl.js",
    "https://cdn.jsdelivr.net/npm/ngl@2.0.0-dev.39/dist/ngl.js",
]

_HOST_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>jamdock-gui 3D viewer</title>
  <style>
    html, body {{ margin: 0; padding: 0; height: 100%; width: 100%;
                  background: #1f2933; overflow: hidden; }}
    #viewport {{ width: 100%; height: 100%; }}
    #status {{ position: absolute; top: 8px; left: 12px;
               color: #ffd166; font-family: sans-serif; font-size: 12px;
               text-shadow: 0 1px 2px black; pointer-events: none;
               max-width: 90%; }}
    #status.error {{ color: #ff6b6b; }}
  </style>
</head>
<body>
  <div id="viewport"></div>
  <div id="status">Loading NGL…</div>
  <script>
  // -- Multi-CDN loader with fallback chain ------------------------------
  const NGL_CDNS = {json.dumps(_NGL_CDNS)};
  function loadNglFromCdn(idx) {{
    if (idx >= NGL_CDNS.length) {{
      const s = document.getElementById("status");
      s.classList.add("error");
      s.textContent = "Failed to load NGL from any CDN. Check your internet "
                      + "connection or firewall (host needs reach to unpkg / jsdelivr).";
      return;
    }}
    const sc = document.createElement("script");
    sc.src = NGL_CDNS[idx];
    sc.crossOrigin = "anonymous";
    sc.onload = function() {{
      if (typeof NGL !== "undefined" && NGL.Stage) {{
        init();
      }} else {{
        loadNglFromCdn(idx + 1);
      }}
    }};
    sc.onerror = function() {{ loadNglFromCdn(idx + 1); }};
    document.head.appendChild(sc);
  }}
  loadNglFromCdn(0);

  // Surface any uncaught JS error in the status bar so the user sees it.
  window.addEventListener("error", function(ev) {{
    const s = document.getElementById("status");
    s.classList.add("error");
    s.textContent = "JS error: " + (ev.message || ev.error || "unknown");
  }});
  </script>
  <script>
  /* ---- jamdock-gui ↔ NGL bridge -------------------------------------- */
  let stage = null;
  let mainComponent = null;
  let pocketShape = null;
  let gridBoxShape = null;
  let currentChains = null;

  function setStatus(text) {{
    const s = document.getElementById("status");
    if (text) {{ s.textContent = text; s.style.display = "block"; }}
    else {{ s.style.display = "none"; }}
  }}

  function init() {{
    if (typeof NGL === "undefined") {{
      setStatus("NGL failed to load.");
      return;
    }}
    stage = new NGL.Stage("viewport", {{
      backgroundColor: "#1f2933",
      tooltip: true,
      cameraType: "perspective",
    }});
    window.addEventListener("resize", () => stage.handleResize(), false);
    setStatus("Drop a PDB or use the wizard…");
  }}

  /* Invoked from Python ------------------------------------------------- */
  window.jamdockLoadPDB = function(pdbText, format) {{
    if (!stage) return;
    setStatus("");
    if (mainComponent) {{
      stage.removeComponent(mainComponent);
      mainComponent = null;
    }}
    const blob = new Blob([pdbText], {{ type: "text/plain" }});
    return stage.loadFile(blob, {{ ext: format || "pdb", defaultRepresentation: false }})
      .then(function(comp) {{
        mainComponent = comp;
        comp.addRepresentation("cartoon", {{
          colorScheme: "chainname",
          smoothSheet: true,
        }});
        comp.addRepresentation("ball+stick", {{
          sele: "ligand and not water",
          aspectRatio: 2.0,
        }});
        comp.autoView(800);
      }})
      .catch(function(err) {{ setStatus("Load failed: " + err.message); }});
  }};

  window.jamdockSetChainSelection = function(chainIds) {{
    if (!mainComponent) return;
    currentChains = chainIds;
    if (!chainIds || chainIds.length === 0) {{
      mainComponent.setVisibility(true);
      return;
    }}
    mainComponent.removeAllRepresentations();
    const sele = chainIds.map(c => ":" + c).join(" or ");
    mainComponent.addRepresentation("cartoon", {{
      sele: sele, colorScheme: "chainname", smoothSheet: true
    }});
    mainComponent.addRepresentation("ball+stick", {{
      sele: "(ligand and not water) and (" + sele + ")",
      aspectRatio: 2.0,
    }});
    mainComponent.autoView(sele, 800);
  }};

  window.jamdockSetPocketSpheres = function(pockets) {{
    if (!stage) return;
    if (pocketShape) {{
      stage.removeComponent(pocketShape);
      pocketShape = null;
    }}
    if (!pockets || pockets.length === 0) return;

    const shape = new NGL.Shape("pockets");
    const palette = [
      [1.0,0.50,0.05], [0.27,0.51,0.71], [0.17,0.63,0.17],
      [0.84,0.15,0.16], [0.58,0.40,0.74], [0.55,0.34,0.29],
      [0.89,0.47,0.76], [0.50,0.50,0.50], [0.74,0.74,0.13],
      [0.09,0.75,0.81],
    ];
    pockets.forEach(function(p, idx) {{
      const c = palette[idx % palette.length];
      const label = "Pocket " + p.number +
                    (p.druggable ? "  (druggable)" : "");
      p.atoms.forEach(function(xyz) {{
        shape.addSphere(xyz, c, 1.2, label);
      }});
    }});
    pocketShape = stage.addComponentFromObject(shape);
    pocketShape.addRepresentation("buffer", {{ opacity: 0.55 }});
  }};

  window.jamdockSetGridBox = function(center, size) {{
    if (!stage) return;
    if (gridBoxShape) {{
      stage.removeComponent(gridBoxShape);
      gridBoxShape = null;
    }}
    if (!center || !size) return;
    const shape = new NGL.Shape("gridbox");
    const cx = center[0], cy = center[1], cz = center[2];
    const sx = size[0]/2, sy = size[1]/2, sz = size[2]/2;
    const corners = [
      [cx-sx, cy-sy, cz-sz], [cx+sx, cy-sy, cz-sz],
      [cx+sx, cy+sy, cz-sz], [cx-sx, cy+sy, cz-sz],
      [cx-sx, cy-sy, cz+sz], [cx+sx, cy-sy, cz+sz],
      [cx+sx, cy+sy, cz+sz], [cx-sx, cy+sy, cz+sz],
    ];
    const edges = [
      [0,1],[1,2],[2,3],[3,0],
      [4,5],[5,6],[6,7],[7,4],
      [0,4],[1,5],[2,6],[3,7],
    ];
    const yellow = [1.0, 0.85, 0.0];
    edges.forEach(function(e) {{
      shape.addCylinder(corners[e[0]], corners[e[1]], yellow, 0.18);
    }});
    gridBoxShape = stage.addComponentFromObject(shape);
    gridBoxShape.addRepresentation("buffer");
  }};

  window.jamdockClear = function() {{
    if (!stage) return;
    stage.removeAllComponents();
    mainComponent = null;
    pocketShape = null;
    gridBoxShape = null;
  }};

  /* init() is invoked by the multi-CDN loader above once NGL is ready. */
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Public widget
# ---------------------------------------------------------------------------
class Viewer3D(QWidget):
    """Embedded NGL viewer with a tiny Python API.

    All ``set_*`` calls are safe to invoke before the page has finished
    loading — they're queued and flushed when ``loadFinished`` fires.
    """

    page_ready = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._stack = QStackedWidget(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

        self._page_ready: bool = False
        self._pending_js: list[str] = []

        if _HAS_WEBENGINE:
            self._view = QWebEngineView(self)
            self._view.setMinimumSize(360, 360)
            self._view.loadFinished.connect(self._on_load_finished)
            self._view.setHtml(_HOST_HTML)
            self._stack.addWidget(self._view)
            self._stack.setCurrentWidget(self._view)
        else:
            self._view = None
            self._stack.addWidget(self._fallback_widget())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def load_pdb(self, pdb_text: str, *, format: str = "pdb") -> None:
        """Load a structure from raw text. *format* is ``pdb`` or ``pdbqt``."""
        self._call_js("jamdockLoadPDB", pdb_text, format)

    def load_pdb_file(self, path: Path | str, *, format: str | None = None) -> None:
        """Convenience wrapper that reads *path* and pushes its contents."""
        path = Path(path)
        if format is None:
            format = "pdbqt" if path.suffix.lower() == ".pdbqt" else "pdb"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("Viewer3D.load_pdb_file: %s", exc)
            return
        self.load_pdb(text, format=format)

    def set_chain_selection(self, chains: Iterable[str] | None) -> None:
        chain_list = list(chains) if chains else []
        self._call_js("jamdockSetChainSelection", chain_list)

    def set_pocket_spheres(self, pockets: Iterable[dict] | None) -> None:
        """Overlay one bundle of small spheres per pocket.

        Each item must be a dict like::

            {"number": 1, "druggable": True,
             "atoms": [(x, y, z), (x, y, z), ...]}
        """
        pocket_list = list(pockets) if pockets else []
        normalised = [
            {
                "number": int(p["number"]),
                "druggable": bool(p.get("druggable", False)),
                "atoms": [list(a) for a in p.get("atoms", [])],
            }
            for p in pocket_list
        ]
        self._call_js("jamdockSetPocketSpheres", normalised)

    def set_grid_box(
        self,
        center: tuple[float, float, float] | None,
        size: tuple[float, float, float] | None,
    ) -> None:
        if center is None or size is None:
            self._call_js("jamdockSetGridBox", None, None)
        else:
            self._call_js("jamdockSetGridBox", list(center), list(size))

    def clear(self) -> None:
        self._call_js("jamdockClear")

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    def _on_load_finished(self, ok: bool) -> None:
        self._page_ready = bool(ok)
        if not ok:
            log.warning("Viewer3D: WebEngine page failed to load.")
            return
        for js in self._pending_js:
            if self._view is not None:
                self._view.page().runJavaScript(js)
        self._pending_js.clear()
        self.page_ready.emit()

    def _call_js(self, fn_name: str, *args: object) -> None:
        """Run ``window.<fn_name>(...args)`` after JSON-serialising args."""
        try:
            payload = ", ".join(json.dumps(a) for a in args)
        except (TypeError, ValueError) as exc:
            log.warning("Viewer3D: cannot serialise %s args: %s", fn_name, exc)
            return
        js = f"window.{fn_name}({payload});"
        if not _HAS_WEBENGINE:
            return
        if self._page_ready and self._view is not None:
            self._view.page().runJavaScript(js)
        else:
            self._pending_js.append(js)

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------
    def _fallback_widget(self) -> QWidget:
        lbl = QLabel(
            "<div style='text-align:center; padding:24px; color:#666'>"
            "<h3>3D viewer unavailable</h3>"
            "<p><code>PySide6.QtWebEngineWidgets</code> is not installed.</p>"
            "<p>Install it to enable the embedded NGL viewer:</p>"
            "<p><code>pip install PySide6-Addons</code></p>"
            "<p style='font-size:smaller; color:#999'>"
            "(The wizard still works — only the 3D preview is missing.)"
            "</p></div>"
        )
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(
            "QLabel { background: #f5f5f5; border: 1px dashed #ccc; }"
        )
        lbl.setWordWrap(True)
        return lbl
