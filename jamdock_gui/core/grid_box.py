"""Grid box geometry for AutoDock Vina / QuickVina 2.

Replicates the Phase 4 logic of ``jamreceptor`` in pure Python so the GUI
can update the wireframe live as the user drags the padding slider.

Output formats supported:
* ``grid.conf`` — Vina-style configuration consumed by ``qvina02 --config``.
* ``grid_box.py`` — PyMOL CGO script (LINE_STRIP) that draws the box.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

#: Default padding (Å) — same as jamreceptor.
DEFAULT_PADDING: float = 10.0


@dataclass(frozen=True)
class GridBox:
    """A Vina grid box — center plus axis-aligned size."""

    center_x: float
    center_y: float
    center_z: float
    size_x: float
    size_y: float
    size_z: float

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------
    @classmethod
    def from_coords(
        cls,
        coords: Iterable[tuple[float, float, float]],
        padding: float = DEFAULT_PADDING,
    ) -> "GridBox":
        """Compute the axis-aligned bounding box of ``coords`` and pad it.

        The padding is added to **each side** identically per axis, matching
        the bash:

        .. code-block::

            size_x = xmax - xmin + PADDING

        Note this is jamreceptor's convention: padding is the *total extra*
        added across the axis, not per face. Keep it consistent with the
        existing scripts so users get the same boxes they're used to.

        Raises:
            ValueError: if ``coords`` is empty.
        """
        xs: list[float] = []
        ys: list[float] = []
        zs: list[float] = []
        for x, y, z in coords:
            xs.append(x)
            ys.append(y)
            zs.append(z)

        if not xs:
            raise ValueError("from_coords requires at least one (x, y, z) tuple.")

        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        zmin, zmax = min(zs), max(zs)

        return cls(
            center_x=(xmin + xmax) / 2.0,
            center_y=(ymin + ymax) / 2.0,
            center_z=(zmin + zmax) / 2.0,
            size_x=(xmax - xmin) + padding,
            size_y=(ymax - ymin) + padding,
            size_z=(zmax - zmin) + padding,
        )

    # ------------------------------------------------------------------
    # Emitters
    # ------------------------------------------------------------------
    def to_vina_conf(self) -> str:
        """Return the contents of ``grid.conf`` (Vina format).

        Centers are rounded to 1 decimal place; sizes to integers — matching
        ``jamreceptor`` so users get bit-identical outputs.
        """
        return (
            f"center_x = {self.center_x:.1f}\n"
            f"center_y = {self.center_y:.1f}\n"
            f"center_z = {self.center_z:.1f}\n"
            f"size_x   = {round(self.size_x):d}\n"
            f"size_y   = {round(self.size_y):d}\n"
            f"size_z   = {round(self.size_z):d}\n"
        )

    def to_pymol_cgo(self) -> str:
        """Return the contents of ``grid_box.py`` — a PyMOL CGO LINE_STRIP."""
        return (
            "from pymol import cmd\n"
            "from pymol.cgo import *\n"
            "\n"
            "# Define grid box dimensions\n"
            f"center_x = {self.center_x:.1f}\n"
            f"center_y = {self.center_y:.1f}\n"
            f"center_z = {self.center_z:.1f}\n"
            f"size_x = {round(self.size_x):d}\n"
            f"size_y = {round(self.size_y):d}\n"
            f"size_z = {round(self.size_z):d}\n"
            "\n"
            "# Calculate the corners of the box\n"
            "x_min = center_x - size_x / 2\n"
            "x_max = center_x + size_x / 2\n"
            "y_min = center_y - size_y / 2\n"
            "y_max = center_y + size_y / 2\n"
            "z_min = center_z - size_z / 2\n"
            "z_max = center_z + size_z / 2\n"
            "\n"
            "# Create a list for the box, properly formatted to avoid line breaks\n"
            "box = [\n"
            "    BEGIN, LINE_STRIP,\n"
            "    VERTEX, x_min, y_min, z_min,\n"
            "    VERTEX, x_max, y_min, z_min,\n"
            "    VERTEX, x_max, y_max, z_min,\n"
            "    VERTEX, x_min, y_max, z_min,\n"
            "    VERTEX, x_min, y_min, z_min,\n"
            "\n"
            "    VERTEX, x_min, y_min, z_max,\n"
            "    VERTEX, x_max, y_min, z_max,\n"
            "    VERTEX, x_max, y_max, z_max,\n"
            "    VERTEX, x_min, y_max, z_max,\n"
            "    VERTEX, x_min, y_min, z_max,\n"
            "\n"
            "    VERTEX, x_min, y_min, z_min,\n"
            "    VERTEX, x_min, y_min, z_max,\n"
            "\n"
            "    VERTEX, x_max, y_min, z_min,\n"
            "    VERTEX, x_max, y_min, z_max,\n"
            "\n"
            "    VERTEX, x_max, y_max, z_min,\n"
            "    VERTEX, x_max, y_max, z_max,\n"
            "\n"
            "    VERTEX, x_min, y_max, z_min,\n"
            "    VERTEX, x_min, y_max, z_max,\n"
            "    END\n"
            "]\n"
            "\n"
            "# Load the grid box into PyMOL\n"
            "cmd.load_cgo(box, 'grid_box')\n"
        )

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------
    def save(self, workdir: Path) -> tuple[Path, Path]:
        """Write ``grid.conf`` and ``grid_box.py`` to ``workdir``.

        Returns the paths of the two written files.
        """
        workdir = Path(workdir)
        conf = workdir / "grid.conf"
        pml = workdir / "grid_box.py"
        conf.write_text(self.to_vina_conf(), encoding="utf-8")
        pml.write_text(self.to_pymol_cgo(), encoding="utf-8")
        return conf, pml
