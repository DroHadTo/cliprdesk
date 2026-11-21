"""Build the plain-source Windows download from the public release manifest."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parent
WORKER = "GTA6cliptool"
FILES = (
    ".env.example", ".gitignore", "CONTRIBUTING.md", "EARNINGS.md", "LICENSE", "MEDIA-TOOLS.md", "OPEN-SOURCE.md", "README.md", "README.txt",
    "SECURITY.md", "THIRD-PARTY-NOTICES.md",
    "ai_models.py", "app.py", "autopost.py", "campaigns_seed.json", "cliptool.py", "creator_rights.py", "earnings_worker.py", "facecam.py", "growth.py", "media_tools.py",
    "models/LICENSE-ultraface.txt", "models/ultraface.onnx", "posts.py", "quality.py",
    "network_policy.py", "requirements.txt", "requirements-media.txt", "research.py", "secret_store.py", "setup.bat", "setup_helper.py",
    "start.bat", "start_hidden.bat", "verify_sha256.py", "virality.py", "web/static/clipdesk.css", "web/static/desk.js",
    "web/static/preview.webp", "whop_api.py", "writer.py", "WORKFLOWS.md",
)
SOURCE_FILES = (
    "build_worker.py", "requirements-test.txt", "tests/test_autopost.py",
    "tests/test_desktop.py",
    "tests/test_earnings_worker.py", "tests/test_growth.py", "tests/test_posts.py",
    "tests/test_quality.py", "tests/test_virality.py", "web/app.py", "web/requirements.txt",
    "web/tests/test_public.py", "web/vercel.json",
)
LAUNCHER_POLICY = re.compile(
    r"\b(?:powershell|pwsh)(?:\.exe)?\b|\b(?:ExecutionPolicy|EncodedCommand|Invoke-Expression"
    r"|Add-MpPreference|Set-MpPreference)\b", re.IGNORECASE,
)
PUBLIC_WATCHLIST = {
    "include": [], "exclude": [], "kick_include": [], "facecam": {},
    "games": ["Grand Theft Auto VI", "Grand Theft Auto V"], "live_top_streams": 20,
}


def add_file(archive, name, contents):
    info = ZipInfo(f"{WORKER}/{name}", date_time=(2026, 11, 21, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    archive.writestr(info, contents, compress_type=ZIP_DEFLATED, compresslevel=9)


def build(output_dir=ROOT / "web" / "static"):
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{WORKER}-worker.zip"
    contents = {name: release_file(name) for name in sorted(FILES)}
    contents["watchlist.json"] = (json.dumps(PUBLIC_WATCHLIST, indent=2, sort_keys=True) + "\n").encode()
    write_archive(destination, contents)
    source_files = {name: release_file(name) for name in SOURCE_FILES}
    write_archive(destination.with_name(f"{WORKER}-source.zip"), contents | source_files)


def release_file(name):
    path = ROOT / name
    if path.resolve() != path or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"Release file must be a regular repository file: {name}")
    if path.suffix in {".onnx", ".webp"}:
        return path.read_bytes()
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".bat":
        commands = [line for line in text.splitlines()
                    if not line.strip().lower().startswith(("rem ", "::"))]
        if any(LAUNCHER_POLICY.search(line) for line in commands):
            raise ValueError(f"Unsupported command in Windows launcher: {name}")
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


def write_archive(destination, contents):
    with TemporaryDirectory(dir=destination.parent) as workspace:
        stage = Path(workspace) / destination.name
        manifest = {name: sha256(data).hexdigest() for name, data in contents.items()}
        with ZipFile(stage, "w") as archive:
            for name, data in contents.items():
                add_file(archive, name, data)
            add_file(archive, "release-manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
        with ZipFile(stage) as archive:
            if archive.testzip():
                raise ValueError("The worker archive failed its integrity check.")
        stage.replace(destination)
    digest = sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(".sha256").write_text(f"{digest}  {destination.name}\n", encoding="ascii")
    print(f"Built {destination.name} with {len(contents) + 1} files. SHA-256 {digest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "web" / "static")
    build(parser.parse_args().output_dir)
