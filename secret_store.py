"""Protect saved credentials with Windows DPAPI under the current user."""
import base64
import ctypes
import io
import os
from threading import RLock
from ctypes import wintypes
from tempfile import NamedTemporaryFile

from dotenv import dotenv_values
from dotenv.parser import parse_stream


KEYS = frozenset({
    "AI_API_KEY", "BUFFER_API_KEY", "IG_RESEARCH_TOKEN", "KICK_CLIENT_SECRET",
    "NTFY_TOPIC", "OLLAMA_API_TOKEN", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "TIKTOK_DISCOVERY_TOKEN", "TWITCH_CLIENT_SECRET", "WHOP_API_KEY",
    "WHOP_CLIENT_SECRET", "YT_RESEARCH_KEY",
})
RETIRED = {"WORKER_TOKEN", "ADMIN_SIGNUP_CODE", "MEMBER_SIGNUP_CODE", "SIGNUP_CODE", "NEW_USER_ROLE", "SITE_URL"}
PREFIX = "dpapi:v1:"
_keys = {}
LOCK = RLock()


class SecretError(ValueError):
    pass


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(value, *, encrypt):
    if os.name != "nt":
        raise SecretError("Saved credentials require Windows user protection.")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    call = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    call.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                     ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    call.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    source = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    incoming, outgoing = Blob(len(value), source), Blob()
    try:
        if not call(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
            raise SecretError("Windows could not protect or unlock a credential for this user.")
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.memset(source, 0, len(value))
        if outgoing.data:
            ctypes.memset(outgoing.data, 0, outgoing.size)
            kernel.LocalFree(outgoing.data)


def protect(value):
    if not value:
        return ""
    return PREFIX + base64.b64encode(_crypt(value.encode(), encrypt=True)).decode("ascii")


def unprotect(value):
    if not value:
        return ""
    if not value.startswith(PREFIX):
        raise SecretError("An unprotected credential must be migrated first.")
    try:
        return _crypt(base64.b64decode(value[len(PREFIX):], validate=True), encrypt=False).decode()
    except (UnicodeError, ValueError) as exc:
        raise SecretError("Unlock the credential from its original Windows account, or enter it again.") from exc


def value(name):
    return os.environ.get(name, _keys.get(name, ""))


def save(path, updates):
    if not updates:
        return
    stored = {key: protect(value) if key in KEYS else value for key, value in updates.items()}
    with LOCK:
        rewrite(path, stored)
        for name, new_value in updates.items():
            if name in KEYS:
                _keys[name] = new_value
                os.environ.pop(name, None)
            else:
                os.environ[name] = new_value


def forget(path, names):
    with LOCK:
        if path.exists() or path.is_symlink():
            rewrite(path, {name: None for name in names})
        for name in names:
            _keys.pop(name, None)
            os.environ.pop(name, None)


def rewrite(path, replacements):
    if path.is_symlink():
        raise SecretError("The settings file must be a regular file in this installation.")
    source = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    parts = [entry.original.string for entry in parse_stream(io.StringIO(source))
             if entry.key not in replacements]
    result = "".join(parts)
    if result and not result.endswith("\n"):
        result += "\n"
    for name, replacement in replacements.items():
        if replacement is not None:
            escaped = replacement.replace("\\", "\\\\").replace("'", "\\'")
            result += f"{name}='{escaped}'\n"
    if result == source:
        return
    stage = None
    try:
        with NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as output:
            stage = output.name
            output.write(result)
            output.flush()
            os.fsync(output.fileno())
        os.replace(stage, path)
    finally:
        if stage and os.path.exists(stage):
            os.unlink(stage)


def initialize(path):
    """Keep provider credentials out of the environment inherited by media tools."""
    disabled = os.environ.get("PYTHON_DOTENV_DISABLED", "").lower() in {"true", "1", "yes", "y", "t"}
    settings = dotenv_values(path, interpolate=False, encoding="utf-8-sig") if not disabled and path.is_file() else {}
    failures, replacements, migrated = [], {}, {}
    for name in RETIRED:
        os.environ.pop(name, None)
        if name in settings:
            replacements[name] = None
    for name, saved in settings.items():
        if name not in KEYS and name not in RETIRED and saved is not None:
            os.environ.setdefault(name, saved)
    for name in KEYS:
        supplied = os.environ.pop(name, "")
        saved = settings.get(name) or ""
        try:
            if saved.startswith(PREFIX):
                _keys[name] = supplied or unprotect(saved)
            elif saved:
                replacements[name] = protect(saved)
                migrated[name] = supplied or saved
                if supplied:
                    _keys[name] = supplied
            elif supplied:
                _keys[name] = supplied
        except (OSError, SecretError):
            if supplied:
                _keys[name] = supplied
            failures.append(name)
    if replacements:
        try:
            rewrite(path, replacements)
        except (OSError, SecretError):
            failures.extend(replacements)
        else:
            _keys.update(migrated)
    return failures
