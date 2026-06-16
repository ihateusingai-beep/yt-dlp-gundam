"""
Nuitka build script — cross-platform portable .exe.
Run: python build_exe.py
Outputs: dist/yt_dlp_gundam.exe  (or dist/yt_dlp_gundam/ on --standalone)
"""
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.resolve()
OUT_DIR   = REPO_ROOT / "dist"
OUT_DIR.mkdir(exist_ok=True)

def get_venv_python():
    """Return python interpreter inside a virtualenv, or fall back to sys.executable."""
    # Check common venv locations
    for venv in [REPO_ROOT / ".venv", REPO_ROOT / "venv"]:
        if venv.exists():
            if sys.platform == "win32":
                python = venv / "Scripts" / "python.exe"
            else:
                python = venv / "bin" / "python"
            if python.exists():
                return str(python)
    return sys.executable


def ensure_packages(python: str, packages: list[str]):
    """Ensure packages are installed in the target environment."""
    for pkg in packages:
        r = subprocess.run([python, "-m", "pip", "show", pkg],
                           capture_output=True)
        if r.returncode != 0:
            subprocess.run([python, "-m", "pip", "install", pkg, "-q"], check=True)


def build_standalone(python: str):
    """Build onefile portable executable via Nuitka."""
    cmd = [
        python, "-m", "nuitka",
        "--standalone",
        "--onefile",
        # Console app (see stdout/stderr)
        "--windows-console-mode=attach",
        # Include templates dir as data
        f"--include-data-dir={REPO_ROOT / 'templates'}=templates",
        # Output
        f"--output-dir={OUT_DIR}",
        # No follow-imports for speed (all deps must be installable)
        "--nofollow-imports",
        # Module mode so it picks up __main__.py logic
        f"{REPO_ROOT / 'main.py'}",
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main():
    python = get_venv_python()
    print(f"Using Python: {python}")

    required = ["nuitka", "yt-dlp", "fastapi", "uvicorn"]
    print("Ensuring packages:", required)
    ensure_packages(python, required)

    build_standalone(python)
    print(f"\nBuild complete. Output in: {OUT_DIR}")


if __name__ == "__main__":
    main()
