"""Open URLs in the system browser with robust WSL2 fallbacks.

Why this exists
---------------
``QDesktopServices.openUrl`` and ``webbrowser.open`` both fail on most
WSL2 setups with::

    qt.qpa.services: Unable to detect a web browser to launch ...

The reason: WSL doesn't have a native Linux browser installed, and Qt
doesn't know how to bridge to the host Windows browser. Meanwhile every
WSL user *does* have a working path to Windows via ``wslview`` (from the
``wslu`` package), ``explorer.exe``, or ``cmd.exe /c start`` — those are
what the terminal uses when you ctrl-click a URL.

This module tries each path in order until one succeeds, so the
"Open ZINC link" button in the Results tab works on WSL, native Linux,
macOS and Windows alike without per-platform code in the GUI.
"""
from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import webbrowser

log = logging.getLogger(__name__)


def _is_wsl() -> bool:
    """Detect WSL1/WSL2. ``uname -r`` is the canonical signal but recent
    distroless / cloud-init kernels sometimes drop the "microsoft" suffix,
    so we also peek at ``/proc/version`` and the WSL-specific env vars
    Microsoft injects."""
    if sys.platform != "linux":
        return False
    try:
        release = os.uname().release.lower()
        if "microsoft" in release or "wsl" in release:
            return True
    except OSError:
        pass
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as fh:
            ver = fh.read().lower()
        if "microsoft" in ver or "wsl" in ver:
            return True
    except OSError:
        pass
    # WSL sets WSL_DISTRO_NAME and WSL_INTEROP on every shell it spawns.
    return bool(os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"))


def _try_spawn(args: list[str]) -> bool:
    """Run *args* detached. Return True on success."""
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
        )
        return True
    except (FileNotFoundError, OSError) as exc:
        log.debug("browser: %s failed (%s)", args[0], exc)
        return False


@contextlib.contextmanager
def _silenced_stderr():
    """Temporarily redirect FD 2 (stderr) to /dev/null at the OS level.

    Python's ``webbrowser.open`` shells out to ``gio open`` / ``xdg-open``
    on Linux. When the desktop integration is half-baked (very common on
    WSL and on minimal Linux installs) those helpers print
    ``gio: <url>: Operation not supported`` to the inherited stderr — and
    that ends up in our GUI's terminal, scaring the user. We can't pass a
    ``stderr=DEVNULL`` kwarg to ``webbrowser.open``, so we redirect the
    file descriptor for the duration of the call.
    """
    if os.name != "posix":
        yield
        return
    try:
        saved = os.dup(2)
    except OSError:
        yield
        return
    try:
        with open(os.devnull, "wb", buffering=0) as devnull:
            os.dup2(devnull.fileno(), 2)
        yield
    finally:
        try:
            os.dup2(saved, 2)
        finally:
            os.close(saved)


def open_url(url: str) -> bool:
    """Open *url* in the user's default browser. Return ``True`` on success.

    Strategy:
        1. On WSL: ``rundll32`` (Windows' canonical URL handler) → ``cmd.exe
           /c start`` → ``wslview`` → ``explorer.exe``.
        2. Native: ``webbrowser.open`` → ``xdg-open`` / ``gio open`` / ``open``.

    Failures are silent (logged at DEBUG) so the caller can surface a
    user-visible message of its choice.
    """
    if not url:
        return False
    # Defensive: Qt's linkActivated signal occasionally hands us URLs with
    # leading/trailing whitespace from clipboard-style flows. Strip it so
    # the Windows-side opener doesn't try to fetch a URL with a stray space
    # at the end (which gets percent-encoded to %20 and trips up servers).
    url = url.strip()
    log.debug("browser: open_url(%r)", url)

    # On WSL, jump straight to the Windows-side openers. We skip
    # ``webbrowser.open`` here because Python's stdlib delegates to
    # ``gio open`` / ``xdg-open`` which return success even when the
    # underlying call fails — so the fallback chain never advances.
    #
    # Order matters here:
    #   * ``rundll32 url.dll,FileProtocolHandler`` is the *exact* Win32 API
    #     that File Explorer's "open link" calls under the hood. It does
    #     not pass through any shell, so it never mangles ``//`` or other
    #     URL-significant characters.
    #   * ``cmd.exe /c start "" URL`` is the next most reliable but ``start``
    #     does some argument parsing that can confuse it with ``&`` URLs.
    #   * ``wslview`` (from the ``wslu`` package) is purpose-built for this
    #     but isn't always installed.
    #   * ``explorer.exe URL`` works most of the time but is *known* to
    #     mangle URLs whose path starts with ``//`` (it can re-interpret
    #     them as UNC paths). That's why it's last.
    if _is_wsl():
        for args in (
            # ``cmd.exe /c start "" URL`` is the most battle-tested form
            # for handing URLs to Windows' default URL handler. The empty
            # ``""`` is the optional window-title argument that ``start``
            # requires when the next argument is quoted.
            ["cmd.exe", "/c", "start", "", url],
            # Win32 URL handler API, called directly. No shell parsing.
            ["rundll32.exe", "url.dll,FileProtocolHandler", url],
            # ``wslu``-provided wrapper, purpose-built for this on WSL.
            ["wslview", url],
            # ``explorer.exe URL`` works most of the time but can
            # misinterpret URLs whose path starts with ``//`` as UNC paths.
            ["explorer.exe", url],
        ):
            if _try_spawn(args):
                log.debug("browser: opened via %s", args[0])
                return True
        # If those all failed, fall through to xdg-open in case the user
        # has a real X-server-side Linux browser installed.

    # Native (Linux/macOS/Windows): Python's stdlib usually wins.
    # We silence stderr while it runs because, on Linux desktops with a
    # broken default-handler registration, ``gio open`` (which webbrowser
    # transitively shells out to) prints
    # ``gio: <url>: Operation not supported`` to stderr even when it
    # ultimately reports success — that leaks into our GUI's terminal.
    try:
        with _silenced_stderr():
            opened = webbrowser.open(url)
        if opened:
            return True
    except (webbrowser.Error, Exception) as exc:    # noqa: BLE001 - defensive
        log.debug("browser: webbrowser.open raised %s", exc)

    # 3) Generic POSIX — try the openers directly so we can pipe their
    # stderr to /dev/null (``_try_spawn`` already does that).
    if sys.platform == "linux":
        for args in (["xdg-open", url], ["gio", "open", url]):
            if _try_spawn(args):
                return True

    # 4) macOS
    if sys.platform == "darwin":
        if _try_spawn(["open", url]):
            return True

    # 5) Windows native (uncommon — webbrowser usually wins here, but in
    # case the user has a weird default-browser registration).
    if sys.platform == "win32":
        try:
            os.startfile(url)   # type: ignore[attr-defined]
            return True
        except OSError as exc:
            log.debug("browser: os.startfile failed (%s)", exc)

    log.warning("browser: no working method to open %s", url)
    return False
