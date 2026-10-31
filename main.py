"""SociaFlow: download publicly accessible media and convert user-uploaded files."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from PIL import Image, ImageOps, UnidentifiedImageError

app = FastAPI(title="SociaFlow", docs_url=None, redoc_url=None)
ROOT = Path(__file__).parent
LIMIT_MB = int(os.environ.get("MAX_UPLOAD_MB", "80"))
MAX_MB = int(os.environ.get("MAX_OUTPUT_MB", "250"))
PROCESS_LIMIT = asyncio.Semaphore(2)
PLATFORMS = {
    "facebook.com": "Facebook", "fb.watch": "Facebook", "instagram.com": "Instagram",
    "threads.com": "Threads", "threads.net": "Threads", "tiktok.com": "TikTok",
    "x.com": "X", "twitter.com": "X", "youtube.com": "YouTube", "youtu.be": "YouTube",
    "reddit.com": "Reddit", "pinterest.com": "Pinterest", "vimeo.com": "Vimeo",
    "dailymotion.com": "Dailymotion"
}
GROUPS = {
    "video": {"mp4", "webm", "mov", "mkv", "avi", "m4v", "mpeg"},
    "audio": {"mp3", "wav", "aac", "m4a", "flac", "ogg", "opus"},
    "image": {"jpg", "jpeg", "png", "webp", "bmp", "gif", "tiff"},
    "pdf": {"pdf"},
}
TARGETS = {
    "video": {"mp4", "webm", "mov", "mp3", "wav", "m4a", "gif"},
    "audio": {"mp3", "wav", "m4a", "ogg", "flac"},
    "image": {"jpg", "png", "webp", "pdf"},
    "pdf": set()
}
FFMPEG_CODEC = {
    "mp4": ["-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p","-c:a","aac"],
    "webm": ["-c:v","libvpx-vp9","-b:v","950k","-c:a","libopus"],
    "mov": ["-c:v","libx264","-preset","veryfast","-c:a","aac"],
    "mp3": ["-vn","-c:a","libmp3lame","-b:a","192k"],
    "wav": ["-vn","-c:a","pcm_s16le"],
    "m4a": ["-vn","-c:a","aac","-b:a","192k"],
    "ogg": ["-vn","-c:a","libvorbis"],
    "flac": ["-vn","-c:a","flac"],
    "gif": ["-t","12","-vf","fps=10,scale=480:-1:flags=lanczos"]
}


def platform_for(url: str) -> str:
    if len(url) > 2048 or not url.startswith("https://"):
        raise HTTPException(400, "Gunakan URL HTTPS dari platform yang didukung.")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("Invalid credentials or port")
    except ValueError:
        raise HTTPException(400, "URL tidak valid.")
    for suffix, name in PLATFORMS.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    raise HTTPException(400, "Link tidak didukung. Gunakan link Facebook, Threads, TikTok, Instagram, YouTube, atau media sosial lainnya.")


def task(args: list[str], timeout=120, cwd=None) -> str:
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout, cwd=cwd, errors="replace")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Proses terlalu lama. Coba video atau format lain.")
    except FileNotFoundError:
        raise HTTPException(503, "Komponen downloader/converter tidak terpasang.")
    if proc.returncode:
        err = proc.stderr.lower()
        if any(word in err for word in ("login", "private", "cookies", "403", "not available")):
            raise HTTPException(422, "Konten tidak bisa diakses secara publik. Coba tautan publik lain.")
        raise HTTPException(422, "Tidak dapat mengambil media. Platform mungkin mengubah format halaman, atau konten tidak tersedia.")
    return proc.stdout


def downloader_args():
    return [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-cache-dir",
            "--socket-timeout", "15", "--retries", "1", "--fragment-retries", "1"]


class Link(BaseModel):
    url: str


class Download(Link):
    kind: str = "video"
    quality: str = "720"


@app.get("/")
def home():
    return FileResponse(ROOT / "webapp" / "index.html", media_type="text/html")


@app.get("/api/health")
def health():
    return {"status": "ok", "ffmpeg": bool(shutil.which("ffmpeg")), "downloader": "yt-dlp"}


@app.post("/api/inspect")
async def inspect(data: Link):
    site = platform_for(data.url)
    async with PROCESS_LIMIT:
        raw = await asyncio.to_thread(task, downloader_args() + ["--dump-single-json", "--skip-download", data.url], 55)
    try:
        info = json.loads(raw)
        if info.get("entries"):
            info = next((x for x in info["entries"] if isinstance(x, dict)), info)
    except (ValueError, TypeError):
        raise HTTPException(422, "Metadata media tidak tersedia.")
    return {
        "platform": site,
        "title": str(info.get("title") or "Postingan publik")[:220],
        "description": str(info.get("description") or "")[:10000],
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail") or "",
        "media": {"video": True, "audio": True, "text": bool(info.get("description"))},
    }


def prepare_download(data: Download, work: Path) -> Path:
    kind = data.kind
    if kind not in ("video", "mp3", "text", "image"):
        raise HTTPException(400, "Pilihan file tidak valid.")
    if kind in ("text", "image"):
        info = json.loads(task(downloader_args() + ["--dump-single-json","--skip-download",data.url], 55))
        if info.get("entries"):
            info = next((x for x in info["entries"] if isinstance(x, dict)), info)
        if kind == "text":
            text = str(info.get("description") or info.get("title") or "")
            if not text:
                raise HTTPException(422, "Caption tidak tersedia.")
            path = work / "sociaflow_caption.txt"
            path.write_text(text[:100000], encoding="utf-8")
            return path
        image = info.get("thumbnail")
        if not image:
            raise HTTPException(422, "Gambar sampul tidak tersedia.")
        # Prevent arbitrary thumbnail fetch: use yt-dlp's extractor and thumbnail facility.
        task(downloader_args() + ["--skip-download","--write-thumbnail","--convert-thumbnails","jpg",
                                  "-o",str(work / "sociaflow.%(ext)s"), data.url], 55)
        thumbnails = list(work.glob("*.jpg"))
        if not thumbnails:
            raise HTTPException(422, "Gambar sampul tidak berhasil diambil.")
        return thumbnails[0]
    if data.quality not in ("best", "360", "480", "720", "1080"):
        raise HTTPException(400, "Kualitas tidak valid.")
    if kind == "mp3":
        fmt = "bestaudio/best"
    elif data.quality == "best":
        fmt = "bv*+ba/b"
    else:
        height = data.quality
        fmt = f"bv*[height<={height}]+ba/b[height<={height}]/best"
    args = downloader_args() + ["--max-filesize",f"{MAX_MB}M","-f",fmt,
            "-o",str(work / "sociaflow.%(ext)s")]
    if kind == "mp3":
        args += ["-x", "--audio-format", "mp3", "--audio-quality", "192K"]
    else:
        args += ["--merge-output-format", "mp4"]
    task(args + [data.url], 210)
    files = [p for p in work.iterdir() if p.is_file() and p.suffix not in (".part", ".ytdl")]
    if not files:
        raise HTTPException(422, "Media tidak berhasil disimpan.")
    result = max(files, key=lambda x: x.stat().st_size)
    if result.stat().st_size > MAX_MB * 1024 * 1024:
        raise HTTPException(413, "File hasil melewati batas ukuran.")
    return result


def response_file(path: Path, folder: Path) -> FileResponse:
    if not path.is_file() or not path.stat().st_size:
        raise HTTPException(422, "Tidak ada file hasil.")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream",
                        background=BackgroundTask(shutil.rmtree, str(folder), ignore_errors=True),
                        headers={"Cache-Control": "no-store"})


@app.post("/api/download")
async def download(data: Download):
    platform_for(data.url)
    folder = Path(tempfile.mkdtemp(prefix="sociaflow_"))
    try:
        async with PROCESS_LIMIT:
            path = await asyncio.to_thread(prepare_download, data, folder)
        return response_file(path, folder)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def convert_file(source: Path, target: str, group: str, folder: Path) -> Path:
    out = folder / f"sociaflow_converted.{target}"
    if group == "image":
        try:
            with Image.open(source) as original:
                Image.MAX_IMAGE_PIXELS = 32_000_000
                img = ImageOps.exif_transpose(original)
                img.load()
                if img.width * img.height > 32_000_000:
                    raise HTTPException(413, "Resolusi gambar terlalu besar.")
                if target == "jpg":
                    canvas = Image.new("RGB", img.size, "white")
                    if "A" in img.getbands(): canvas.paste(img, mask=img.getchannel("A"))
                    else: canvas.paste(img.convert("RGB"))
                    canvas.save(out, "JPEG", quality=90)
                elif target == "pdf":
                    img.convert("RGB").save(out, "PDF")
                elif target == "webp":
                    img.save(out, "WEBP", quality=88)
                else:
                    img.save(out, "PNG")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise HTTPException(422, "File gambar rusak atau tidak didukung.")
    else:
        task(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
             + FFMPEG_CODEC[target] + [str(out)], 160)
    if not out.is_file() or out.stat().st_size > MAX_MB * 1024 * 1024:
        raise HTTPException(413, "Output terlalu besar atau tidak berhasil dibuat.")
    return out


@app.post("/api/convert")
async def convert(file: UploadFile = File(...), target: str = Form(...)):
    suffix = Path(file.filename or "").suffix.lower().lstrip(".")
    group = next((g for g, extensions in GROUPS.items() if suffix in extensions), None)
    if not group or target not in TARGETS[group]:
        raise HTTPException(400, "Kombinasi format tidak didukung.")
    folder = Path(tempfile.mkdtemp(prefix="sociaflow_convert_"))
    try:
        source = folder / f"upload.{suffix}"
        total = 0
        with source.open("wb") as fh:
            while chunk := await file.read(1024*1024):
                total += len(chunk)
                if total > LIMIT_MB * 1024*1024:
                    raise HTTPException(413, f"Ukuran maksimal {LIMIT_MB} MB.")
                fh.write(chunk)
        if not total:
            raise HTTPException(400, "File kosong.")
        async with PROCESS_LIMIT:
            output = await asyncio.to_thread(convert_file, source, target, group, folder)
        return response_file(output, folder)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        await file.close()
