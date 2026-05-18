"""Application bootstrap: creates QApplication and shows MainWindow.

WSL / sandbox compatibility note
--------------------------------
Qt WebEngine (which we use for the embedded NGL 3D viewer) ships its own
Chromium build that, by default, expects a working user-namespace sandbox.
On WSL2, hardened Linux kernels, and some container distros that sandbox
isn't available, which causes QtWebEngine to silently fail to render or
crash the renderer process.

We detect those environments at startup and set the matching Chromium
flags **before** any QtWebEngine module is imported. This MUST happen
before ``from PySide6.QtWebEngineWidgets import …`` anywhere in the
process — that's why the env tweak lives here in :func:`main`, the very
first code that runs.
"""
from __future__ import annotations

import os
import sys


def _patch_webengine_for_sandbox_environments() -> None:
    """Add ``--no-sandbox`` (and friends) to QtWebEngine on WSL/containers.

    No-op on macOS, Windows, and ordinary Linux desktops. Honours any pre-set
    ``QTWEBENGINE_CHROMIUM_FLAGS`` so an advanced user can override us.
    """
    if sys.platform != "linux":
        return

    is_wsl = "microsoft" in os.uname().release.lower()
    is_container = os.path.exists("/.dockerenv")
    # Some hardened distros disable user-namespace sandboxing system-wide.
    user_ns_disabled = False
    sysctl_path = "/proc/sys/kernel/unprivileged_userns_clone"
    if os.path.isfile(sysctl_path):
        try:
            with open(sysctl_path, "r", encoding="ascii") as fh:
                user_ns_disabled = fh.read().strip() == "0"
        except OSError:
            pass

    if not (is_wsl or is_container or user_ns_disabled):
        return

    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    needed = [
        # -- Sandboxing -------------------------------------------------
        # The user-namespace sandbox isn't available in WSL.
        "--no-sandbox",
        "--disable-gpu-sandbox",
        # Avoids "/dev/shm too small" crashes in containers.
        "--disable-dev-shm-usage",
        # -- GPU / WebGL ------------------------------------------------
        # WSLg exposes a partial Vulkan/dma-buf surface that Chromium tries
        # to use and fails on (Compositor returned null texture). Turn off
        # GPU acceleration entirely so the compositor stays in software,
        # and let SwiftShader pick up WebGL as the only renderer. This is
        # noticeably slower than a real GPU but it's the only path that
        # works on WSL2/WSLg today.
        "--disable-gpu",
        "--disable-gpu-compositing",
        "--ignore-gpu-blocklist",
        "--enable-webgl",
        # ``--enable-unsafe-swiftshader`` is needed on Chromium ≥ 120 to
        # whitelist the SwiftShader fallback after our other GPU bypass.
        "--enable-unsafe-swiftshader",
    ]
    additions = [f for f in needed if f not in flags]
    if additions:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(
            ([flags] if flags else []) + additions
        ).strip()
    # Also disable the sandbox via the dedicated env knob (belt & braces).
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    # Force Mesa to its software path BEFORE Chromium queries libGL — kills
    # the "MESA-LOADER" / dma_buf warnings and the ZINK driver attempts.
    os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
    # Disable Vulkan probing: WSLg's Vulkan driver isn't compatible with
    # Chromium's compositor.
    os.environ.setdefault("QT_QUICK_BACKEND", "software")


# Run the patch *before* importing anything Qt-related.
_patch_webengine_for_sandbox_environments()

# noqa: E402 — late imports are intentional; see module docstring.
from PySide6.QtCore import QCoreApplication           # noqa: E402
from PySide6.QtWidgets import QApplication            # noqa: E402

from jamdock_gui import APP_NAME, APP_ORG, __version__  # noqa: E402
from jamdock_gui.main_window import MainWindow          # noqa: E402


def main() -> int:
    """Entry point used by the ``jamdock-gui`` console script."""
    QCoreApplication.setOrganizationName(APP_ORG)
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setApplicationVersion(__version__)

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
