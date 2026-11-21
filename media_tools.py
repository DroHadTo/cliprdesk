"""Import owned VOD cuts and export assets for timeline editors."""
import argparse
from contextlib import closing
import hashlib
from importlib.util import find_spec
import json
import math
from pathlib import Path
import re
from shutil import which
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from zipfile import ZipFile

import cliptool as ct


VOD_PATHS = {
    "www.twitch.tv": r"/videos/\d+/?",
    "twitch.tv": r"/videos/\d+/?",
    "www.youtube.com": r"/watch",
    "youtube.com": r"/watch",
    "youtu.be": r"/[A-Za-z0-9_-]{11}/?",
}


def validate_vod_url(url):
    parsed = urlsplit(url)
    path = VOD_PATHS.get(parsed.netloc.lower())
    if parsed.scheme != "https" or not path or not re.fullmatch(path, parsed.path) or any(c.isspace() for c in url):
        raise ValueError("Use an HTTPS Twitch VOD or YouTube video URL.")
    return parsed


def import_vod(url, streamer, title, start, length, game):
    parsed = validate_vod_url(url)
    if not math.isfinite(start) or start < 0 or length not in ct.CLIP_LENGTHS:
        raise ValueError("Choose a non-negative start and a supported clip length.")
    if not find_spec("yt_dlp"):
        raise ValueError("Install the optional media requirements to use VOD import.")
    clip_id = hashlib.blake2s(f"{url}|{start}|{length}".encode(), digest_size=12).hexdigest()
    source = "Twitch" if "twitch" in parsed.netloc else "YouTube"
    js_runtime = ["--js-runtimes", "node"] if source == "YouTube" and which("node") else []
    with TemporaryDirectory(dir=ct.DATA) as temp:
        media = Path(temp) / "source.mp4"
        result = subprocess.run([
            sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", *js_runtime,
            "--downloader", "ffmpeg", "--ffmpeg-location", ct.FFMPEG,
            "--download-sections", f"*{start:.2f}-{start+length:.2f}",
            "--force-keyframes-at-cuts", "-f", "bv*[height<=?720][ext=mp4]+ba[ext=m4a]/b[height<=?720]",
            "--merge-output-format", "mp4", "--remux-video", "mp4", "-o", str(media), "--", url,
        ], timeout=600, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=ct.NOWIN)
        if result.returncode:
            raise ValueError("yt-dlp could not fetch the VOD section. " + result.stderr.strip()[-500:])
        if not media.is_file():
            raise ValueError("The VOD cut did not produce an MP4.")
        return ct.make_clip(dict(id=f"vod-import-{clip_id}", source="vod-import", platform=source,
                                 streamer=streamer, title=title, url=url, game=game,
                                 local=str(media), dur=length, peak=min(8, length / 3)))


def export_project(clip, destination):
    path = (ct.OUT / clip["file"]).resolve()
    if not path.is_relative_to(ct.OUT.resolve()) or not path.is_file():
        raise ValueError("The selected export is missing.")
    stamp = path.stat().st_mtime_ns
    selected = ct.edit_metadata(path)
    style = clip["pick"] or ""
    original = path.with_name("meta.json")
    subtitles = path.with_name(f"tiktok_{style}.ass")
    meta = json.loads(original.read_text(encoding="utf-8")) if original.is_file() else {}
    if not subtitles.is_file() and meta:
        if all(key in selected and selected[key] == meta.get(key) for key in ("start", "dur")):
            subtitles = path.with_name("captions.ass")
    editly = {"outPath": "edited.mp4", "width": 1080, "height": 1920, "fps": 30,
              "keepSourceAudio": True, "clips": [{"layers": [{"type": "video", "path": "clip.mp4"}]}]}
    info = {"clip_id": clip["id"], "creator": clip["streamer"], "source_url": clip["url"],
            "layout": style, "selected_cut": {key: selected.get(key) for key in ("start", "dur", "peak")}}
    with ZipFile(destination, "x") as archive:
        archive.write(path, "clip.mp4")
        if meta.get("raw"):
            source = Path(meta["raw"]).resolve()
            if source.parent == path.parent and source.is_file() and source != path:
                archive.write(source, "source.mp4")
        archive.writestr("clip.json", json.dumps(info, ensure_ascii=False, indent=2))
        archive.writestr("editly.json5", json.dumps(editly, indent=2))
        transcript = path.with_name(f"transcript_{style}.txt" if style else "transcript.txt")
        if transcript.is_file():
            archive.write(transcript, "transcript.txt")
        if subtitles.is_file():
            archive.write(subtitles, "captions.ass")
            if find_spec("pysubs2"):
                import pysubs2

                subs = pysubs2.load(str(subtitles), encoding="utf-8")
                archive.writestr("captions.srt", subs.to_string("srt"))
                archive.writestr("captions.vtt", subs.to_string("vtt"))
        if path.stat().st_mtime_ns != stamp:
            raise ValueError("The clip changed while exporting. Download the selected edit again.")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="action", required=True)
    vod = commands.add_parser("vod", help="Import a cut you have permission to edit.")
    vod.add_argument("url")
    vod.add_argument("--creator", required=True)
    vod.add_argument("--title", required=True)
    vod.add_argument("--game", default="")
    vod.add_argument("--start", type=float, required=True)
    vod.add_argument("--length", type=int, choices=ct.CLIP_LENGTHS, default=45)
    package = commands.add_parser("package", help="Export a clip and its edit assets as a ZIP.")
    package.add_argument("clip_id")
    package.add_argument("output", type=Path)
    args = cli.parse_args()
    try:
        if args.action == "vod":
            print(import_vod(args.url, args.creator, args.title, args.start, args.length, args.game)
                  or "No new edit was made. Check the clip log.")
        else:
            with closing(ct.db()) as conn:
                clip = conn.execute("SELECT * FROM clips WHERE id=?", (args.clip_id,)).fetchone()
            if not clip:
                raise ValueError("Clip ID was not found.")
            with TemporaryDirectory(dir=args.output.resolve().parent) as directory:
                package_path = Path(directory) / args.output.name
                export_project(clip, package_path)
                if args.output.exists():
                    raise ValueError("The destination already exists.")
                package_path.rename(args.output.resolve())
            print(args.output)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        cli.exit(1, f"Media job failed: {exc}\n")


if __name__ == "__main__":
    main()
