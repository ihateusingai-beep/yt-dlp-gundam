"""
System tray icon for yt-dlp Gundam Dashboard.
- Single click on icon: open dashboard in default browser
- Right-click menu: Open / Show status / Quit
- Auto-loads icon from bundled .ico / .png (PyInstaller-friendly path)
"""
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path


def _get_icon_path() -> str | None:
    """Locate the tray icon. In frozen mode, look in sys._MEIPASS."""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).parent.resolve()
    for name in ("ntd_icon.ico", "ntd_icon.png"):
        candidate = base / name
        if candidate.exists():
            return str(candidate)
    return None


def _default_icon():
    """Generate a fallback icon in memory if no .ico/.png shipped."""
    try:
        from PIL import Image, ImageDraw
        size = 64
        img = Image.new("RGBA", (size, size), (10, 15, 26, 255))
        d = ImageDraw.Draw(img)
        d.rectangle([size//2 - 4, 8, size//2 + 4, size - 8], fill=(255, 105, 180, 255))
        d.rectangle([8, size//2 - 4, size - 8, size//2 + 4], fill=(0, 212, 255, 255))
        return img
    except Exception:
        return None


def run_tray(port: int, stop_event: threading.Event):
    """Start a pystray icon in the current thread. Blocks until icon.stop()
    is called. Use threading.Thread(target=run_tray, daemon=True) to fire-and-go.

    Args:
        port: HTTP port the server is bound to
        stop_event: threading.Event the caller can .set() to signal shutdown
                    (pystray's blocking run loop will be torn down from a worker)
    """
    import pystray
    from PIL import Image

    icon_path = _get_icon_path()
    if icon_path and icon_path.lower().endswith(".ico"):
        image = Image.open(icon_path)
    elif icon_path:
        image = Image.open(icon_path)
    else:
        image = _default_icon()
    if image is None:
        # Last resort — empty image, will still register a menu
        image = Image.new("RGB", (16, 16), "black")

    url = f"http://localhost:{port}"

    def on_open(icon, item):
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def on_status(icon, item):
        """Quick native notification showing port + URL.

        Platform implementations:
          - Windows: PowerShell + .NET NotifyIcon balloon (no extra deps —
            PowerShell is built into every supported Windows version, and
            System.Windows.Forms.NotifyIcon ships with .NET Framework).
          - macOS:   osascript (Apple's standard notification path).
          - Linux:   silent no-op (no portable cross-DE notification API).
        """
        try:
            if sys.platform == "win32":
                # PowerShell script, parameterized via $args[0] so `url` is
                # passed as a separate argv entry — no shell-string injection
                # even if a future change ever lets the port come from
                # untrusted input. `Start-Sleep` + `Dispose` keeps the
                # balloon visible ~5 s, then cleans up.
                ps_script = (
                    "param($u) "
                    "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
                    "Add-Type -AssemblyName System.Drawing | Out-Null; "
                    "$n = New-Object System.Windows.Forms.NotifyIcon; "
                    "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                    "$n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info; "
                    "$n.BalloonTipTitle = 'yt-dlp Gundam'; "
                    "$n.BalloonTipText = \"Dashboard: $u\"; "
                    "$n.Visible = $true; "
                    "$n.ShowBalloonTip(5000); "
                    "Start-Sleep -Milliseconds 5500; "
                    "$n.Visible = $false; "
                    "$n.Dispose()"
                )
                # Popen (not run) so the tray thread stays responsive while
                # the balloon is on-screen. CREATE_NO_WINDOW keeps the
                # PowerShell console hidden. We deliberately don't wait —
                # if the user spam-clicks, multiple balloons are fine.
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command", ps_script, url],
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif sys.platform == "darwin":
                # v0.8.4 — use subprocess.Popen with argv instead of
                # os.system + f-string interpolation. osascript's Apple-
                # Event handler tolerates most chars in the URL but the
                # shell-quoting layer above was fragile (backslash, single
                # quote, $() expansion). subprocess with argv avoids the
                # shell entirely; osascript gets the literal string.
                # Popen (not run) so the tray thread stays responsive
                # while osascript launches the notification center UI.
                script = (
                    f'display notification "Dashboard: {url}" '
                    f'with title "yt-dlp Gundam" subtitle "Server running"'
                )
                subprocess.Popen(
                    ["osascript", "-e", script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            # else: Linux / other — silent no-op (no portable API).
        except Exception:
            # Notification is best-effort; never break the tray over a failed toast.
            pass

    def on_quit(icon, item):
        stop_event.set()
        try:
            icon.stop()
        except Exception:
            pass

    def on_reveal(icon, item):
        """Open the downloads folder in the OS file manager."""
        downloads = Path(sys.executable).parent / "downloads" if getattr(sys, "frozen", False) \
            else Path(__file__).parent / "downloads"
        downloads.mkdir(exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(downloads))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{downloads}"')
            else:
                os.system(f'xdg-open "{downloads}"')
        except Exception:
            pass

    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", on_open, default=True),
        pystray.MenuItem(url, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Downloads Folder", on_reveal),
        pystray.MenuItem("Show Status", on_status),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Server", on_quit),
    )

    icon = pystray.Icon(
        name="yt_dlp_gundam",
        icon=image,
        title="yt-dlp Gundam",
        menu=menu,
    )

    # pystray.Icon.run() blocks. We run it directly in this thread.
    icon.run()
