"""
System tray icon for yt-dlp Gundam Dashboard.
- Single click on icon: open dashboard in default browser
- Right-click menu: Open / Show status / Quit
- Auto-loads icon from bundled .ico / .png (PyInstaller-friendly path)
"""
import os
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
        # Quick native notification showing port + URL
        try:
            if sys.platform == "win32":
                from win10toast import ToastNotifier  # type: ignore
            elif sys.platform == "darwin":
                # Use osascript to display a native macOS notification
                os.system(
                    f'osascript -e \'display notification "Dashboard: {url}" '
                    f'with title "yt-dlp Gundam" subtitle "Server running"\''
                )
                return
            else:
                return
        except Exception:
            return

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
