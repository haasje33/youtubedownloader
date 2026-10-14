"""System notifications when downloads and upscales finish.

macOS: Notification Center via `osascript` (works for unsigned apps).
Windows: a toast via the WinRT API from Windows PowerShell. Toasts appear under
the app's name because the Start menu shortcut carries the same AppUserModelID
that the app sets for itself (see gui._windows_setup and installer.iss).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from xml.sax.saxutils import escape

from . import APP_NAME, tools

log = logging.getLogger(__name__)

APP_USER_MODEL_ID = "YouTubeDownloader.App"


def _mac(title: str, message: str) -> list[str]:
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    return ["osascript", "-e", script]


def _windows(title: str, message: str) -> list[str]:
    xml = (f"<toast><visual><binding template='ToastGeneric'><text>{escape(title)}</text>"
           f"<text>{escape(message)}</text></binding></visual></toast>")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$xml.LoadXml('{xml.replace(chr(39), chr(39) * 2)}');"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_USER_MODEL_ID}').Show($toast)"
    )
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]


def command(title: str, message: str, platform: str | None = None) -> list[str] | None:
    platform = platform or sys.platform
    if platform == "darwin":
        return _mac(title, message)
    if platform == "win32":
        return _windows(title, message)
    return None


def notify(message: str, title: str = APP_NAME) -> None:
    """Show a notification without blocking (failures are only logged)."""
    cmd = command(title, message)
    if cmd is None:
        return

    def run() -> None:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False,
                                    **tools.no_window())
            if result.returncode != 0:
                log.info("Notification failed: %s", (result.stderr or result.stdout).strip()[:300])
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("Notification failed: %s", exc)

    threading.Thread(target=run, daemon=True).start()
