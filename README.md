# 🎬 yt-dlp Gundam Dashboard

Gundam NT-D cockpit-style video downloader. Download YouTube/videos without command line.

## Features

- 🎯 URL input → video preview (title, thumbnail, duration)
- 📺 MP4 (best quality) / 🎵 MP3 (audio only)
- ⚡ Real-time progress bar
- 🎉 Completion celebration
- 🔧 FFmpeg auto-detection

## Setup

### 1. Install FFmpeg

**Windows (Scoop):**
```powershell
scoop install ffmpeg
```

**Windows (Direct):**
Download from https://ffmpeg.org/download.html

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

## Tech

- FastAPI + Jinja2
- yt-dlp (video extraction)
- Pure HTML/CSS/JS (no build step)
- Gundam NT-D theme (psychoframe pink + cyan)

## License

Unlicense