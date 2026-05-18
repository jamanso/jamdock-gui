"""Smoke tests — fail fast on a broken install.

These tests don't exercise behaviour, they just make sure the package
installs cleanly and that every public module can be imported. They are
the first line of defence against silly mistakes like a typo in
``pyproject.toml``, a missing ``__init__.py``, or an import cycle.

The CI workflow runs them first; if they fail the rest of the suite is
not even attempted.
"""
from __future__ import annotations

import jamdock_gui


def test_version_is_a_non_empty_string() -> None:
    """``__version__`` must be set and non-empty — packaging metadata
    derives from it."""
    assert isinstance(jamdock_gui.__version__, str)
    assert jamdock_gui.__version__.strip()


def test_app_constants_present() -> None:
    """``APP_NAME`` and ``APP_ORG`` feed into ``QSettings`` paths. Make
    sure they exist and are non-empty."""
    assert getattr(jamdock_gui, "APP_NAME", "")
    assert getattr(jamdock_gui, "APP_ORG", "")


def test_core_modules_import() -> None:
    """All non-GUI modules under ``jamdock_gui.core`` must import
    without side effects. A new import error here means a typo or a
    missing dep got merged."""
    # Listed explicitly (not autodiscovered) so the failure mode is
    # "module X is broken", not "the test scanner found nothing".
    from jamdock_gui.core import (  # noqa: F401
        browser,
        docking,
        grid_box,
        pdb_clean,
        pocket,
        process_runner,
        pymol_launcher,
        qvina_log,
        receptor_prep,
        results,
        script_paths,
        state,
        waters,
    )
