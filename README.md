# 🎬 yt-dlp Gundam Dashboard

Gundam NT-D cockpit-style video downloader. Download YouTube/videos without command line.

## Features

- 🎯 URL input → video preview (title, thumbnail, duration)
- 📺 MP4 (best quality) / 🎵 MP3 (audio only)
- ⚡ Real-time progress bar
- 🎉 Completion celebration
- 🔧 FFmpeg auto-detection
- 🌐 Remote access via Tailscale (Windows + Mac)
- 📦 Portable .exe (no Python installation required)

## Setup

### 1. Install FFmpeg

**Windows (Scoop):**
```powershell
scoop install ffmpeg
```

**Windows (Direct):**
Download from https://ffmpeg.org/download.html

**macOS:**
```bash
brew install ffmpeg
```

### 2. Install Dependencies

```powershell
pip install -r requirements.txt
```

### 3. Run

```powershell
python main.py
```

### 4. Open Browser

```
http://localhost:8000
```

## Remote Access via Tailscale

Access the dashboard from any device on your Tailscale network (including your Windows PC from your Mac, or vice versa).

### Setup (one-time)

1. **Install Tailscale on all devices:**
   - Mac: `brew install tailscale` then run Tailscale from Applications
   - Windows: download from https://tailscale.com/download/windows

2. **Login to Tailscale** on both devices (same Tailnet/account).

3. **Find your Tailscale IP:**
   - Mac: click the Tailscale menu bar icon → IP address (e.g. `100.x.x.x`)
   - Windows: same

4. **Start the server on Mac:**
   ```bash
   cd ~/workspace/yt-dlp-gundam
   python main.py
   ```

5. **Access from Windows:**
   ```
   http://<mac-tailscale-ip>:8000
   ```

> Note: The server binds to `0.0.0.0:8000` so it's accessible via your Tailscale IP. Make sure your Mac's firewall allows inbound connections on port 8000 (Tailscale traffic is allowed by default).

## Build Portable .exe (Windows)

Build a standalone Windows executable — no Python installation needed on the target PC.

### Prerequisites

```powershell
pip install pyinstaller yt-dlp
```

### Build

```powershell
# From the project directory
pyinstaller yt_dlp_gundam.spec
```

The portable `.exe` will be in `dist/yt_dlp_gundam/`.

### Run the .exe

1. Copy the entire `dist/yt_dlp_gundam/` folder to the Windows PC.
2. (Optional) Install FFmpeg on that PC if you need video transcoding.
3. Double-click `yt_dlp_gundam.exe`.
4. Open `http://localhost:8000` in your browser.

## Tech

- FastAPI + Jinja2
- yt-dlp (video extraction)
- Pure HTML/CSS/JS (no build step)
- Gundam NT-D theme (psychoframe pink + cyan)
- PyInstaller (optional portable build)

## License

Unlicense