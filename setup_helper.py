"""Prepare the Windows worker from checked-in setup files and a pinned release."""
import argparse
import hashlib
import shutil
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile


ROOT = Path(__file__).resolve().parent
TDCLI_EXE = "TwitchDownloaderCLI.exe"
TDCLI_FILES = ("COPYRIGHT.txt", "THIRD-PARTY-LICENSES.txt", TDCLI_EXE)
TDCLI_URL = "https://github.com/lay295/TwitchDownloader/releases/download/1.56.5/TwitchDownloaderCLI-1.56.5-Windows-x64.zip"
TDCLI_SHA256 = "8b1b0695f2b1b6bf0d2535fab4b84032951cded8cf4078dfdf4d58e391c813a0"
TDCLI_ARCHIVE_BYTES = 52_226_305


class SetupError(Exception):
    pass


class HTTPSRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise SetupError("The release download redirected to a non-HTTPS address.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_release(archive):
    sha256 = hashlib.sha256()
    size = 0
    request = Request(TDCLI_URL, headers={"User-Agent": "GTA6cliptool setup"})
    with build_opener(HTTPSRedirects()).open(request, timeout=30) as response, archive.open("xb") as output:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > TDCLI_ARCHIVE_BYTES:
                raise SetupError("TwitchDownloaderCLI download size did not match the pinned release.")
            sha256.update(chunk)
            output.write(chunk)
    if size != TDCLI_ARCHIVE_BYTES or sha256.hexdigest() != TDCLI_SHA256:
        raise SetupError("TwitchDownloaderCLI download did not match the pinned SHA-256 and size.")


def install_tdcli(directory):
    directory.mkdir(exist_ok=True)
    print("Downloading TwitchDownloaderCLI 1.56.5 from its GitHub release.", flush=True)
    with TemporaryDirectory(dir=directory) as temp_dir:
        stage = Path(temp_dir)
        archive = stage / "twitchdownloader.zip"
        download_release(archive)
        with ZipFile(archive) as release:
            for name in TDCLI_FILES:
                matches = [member for member in release.infolist() if member.filename == name]
                if len(matches) != 1:
                    raise SetupError(f"Expected one {name} in the verified release.")
                member = matches[0]
                if member.is_dir() or stat.S_IFMT(member.external_attr >> 16) not in (0, stat.S_IFREG):
                    raise SetupError(f"Expected a regular file for {name} in the verified release.")
                with release.open(member) as source, (stage / name).open("xb") as output:
                    shutil.copyfileobj(source, output)
        # Publish the executable last so a retry can recover from an interrupted install.
        for name in TDCLI_FILES:
            (stage / name).replace(directory / name)
    print("Verified and installed TwitchDownloaderCLI.")


def prepare_env():
    env = ROOT / ".env"
    if not env.exists():
        shutil.copyfile(ROOT / ".env.example", env)
    print(".env ready. Connect only the providers you need from Clip Desk Setup.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("env", "tdcli"))
    try:
        if parser.parse_args().action == "tdcli":
            install_tdcli(ROOT / "bin")
        else:
            prepare_env()
    except (OSError, BadZipFile, SetupError) as exc:
        parser.exit(1, f"Setup did not complete: {exc}\n")


if __name__ == "__main__":
    main()
