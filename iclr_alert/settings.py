"""Runtime configuration and Windows-protected local session storage."""

import base64
import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from .core import ProbeError, parse_forum_id
from .web import PRIVATE_DIR, read_json, write_json

SESSION_PATH = PRIVATE_DIR / "openreview-session.json"
DEFAULT_PAPER = ""


def settings():
    local = read_json(PRIVATE_DIR / "config.json")
    if not isinstance(local, dict):
        raise ProbeError("invalid_config", "本机配置格式不正确。")
    token = os.environ.get("OPENREVIEW_TOKEN") or load_session() or local.get("openreview_token", "")
    data_dir = Path(os.environ.get("DATA_DIR", str(PRIVATE_DIR)))
    try:
        interval = int(os.environ.get("CHECK_INTERVAL_SECONDS", "120"))
    except ValueError:
        raise ProbeError("invalid_config", "CHECK_INTERVAL_SECONDS 必须是整数秒。") from None
    if not 60 <= interval <= 86400:
        raise ProbeError("invalid_config", "检查间隔应在 60 秒至 1 天之间。")
    return {
        "paper": os.environ.get("OPENREVIEW_PAPER") or local.get("paper") or DEFAULT_PAPER,
        "token": token,
        "bark_key": os.environ.get("BARK_DEVICE_KEY") or local.get("bark_key", ""),
        "interval": interval,
        "data_dir": data_dir,
        "db_path": data_dir / "monitor.sqlite3",
    }


def require_paper(config):
    """Require an explicit target; a fresh installation never monitors an author's paper."""
    paper = config.get("paper", "")
    if not paper:
        raise ProbeError("missing_paper", "请先运行 iclr-alert paper <论文链接或 forum ID>，或设置 OPENREVIEW_PAPER。")
    return "https://openreview.net/forum?id=" + parse_forum_id(paper)


def save_session(token):
    if not token or not isinstance(token, str):
        raise ProbeError("invalid_session", "登录未返回有效会话。")
    protected = _dpapi(token.encode("utf-8"), protect=True)
    write_json(SESSION_PATH, {"protection": "windows-dpapi-current-user", "protected_token": base64.b64encode(protected).decode("ascii")})


def load_session():
    value = read_json(SESSION_PATH)
    if not value:
        return ""
    try:
        protected = base64.b64decode(value["protected_token"], validate=True)
        return _dpapi(protected, protect=False).decode("utf-8")
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ProbeError("invalid_session", "本机会话损坏，请重新登录。") from None


def _dpapi(data, *, protect):
    if os.name != "nt":
        raise ProbeError("unsupported_session_store", "本机加密会话仅适用于 Windows；云端请使用 OPENREVIEW_TOKEN Secret。")

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = DataBlob(len(data), buffer)
    destination = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.restype = wintypes.BOOL
    function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    # UI_FORBIDDEN, and no LOCAL_MACHINE flag: only the current Windows user.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(destination)):
        raise ProbeError("session_store_failed", "无法保护或读取本机会话，请在你自己的 Windows 会话中重新登录。")
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        kernel32.LocalFree(destination.data)
