import asyncio
import os
import re
import shutil
from pathlib import Path

import yt_dlp

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "data/downloads"))
MAX_MB = int(os.getenv("MAX_DOWNLOAD_MB", "45"))

def safe_name(name: str) -> str:
    name = re.sub(r"[^\w\-. ]+", "_", name, flags=re.UNICODE).strip()
    return name[:80] or "video"

def _download(url: str):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    outtmpl = str(DOWNLOAD_DIR / "%(id)s_%(title).80s.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
        if path.suffix.lower() != ".mp4":
            candidates = list(DOWNLOAD_DIR.glob(f"{info.get('id','')}*"))
            if candidates:
                path = max(candidates, key=lambda p: p.stat().st_mtime)
        return path, info

async def download(url: str):
    return await asyncio.to_thread(_download, url)

def remove_file(path: Path):
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
