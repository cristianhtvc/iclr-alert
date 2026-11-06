"""Normal API v2 login following the official openreview-py login/MFA protocol.

This module returns only the issued session token. It never persists passwords,
reads browser sessions, or attempts to solve an access-verification challenge.
Protocol: openreview/openreview-py, openreview/api/client.py, login_user().
"""

import json
import re
from urllib.request import Request

from .core import API_BASE, ProbeError, _request_json

SESSION_SECONDS = 604800


def login(username: str, password: str, otp_provider=None) -> dict:
    """Return {'token': ...}; optional provider(method) supplies email/TOTP code.

    The caller owns any local session-token persistence. A requested one-week
    lifetime is not an asserted expiry time; the server controls token validity.
    """
    if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
        raise ProbeError("invalid_credentials", "请填写 OpenReview 登录邮箱和密码。")
    try:
        response = _post("/login", {"id": username.strip(), "password": password, "expiresIn": SESSION_SECONDS})
    except ProbeError as error:
        if error.code == "unauthorized":
            raise ProbeError("invalid_credentials", "OpenReview 登录未通过，请核对邮箱和密码。") from None
        raise
    if response.get("mfaPending"):
        response = _complete_mfa(response, otp_provider)
    token = response.get("token")
    if not isinstance(token, str) or not token or len(token) > 16384 or any(ord(char) <= 32 for char in token):
        raise ProbeError("invalid_response", "OpenReview 未返回有效的登录会话。")
    return {"token": token}


def _complete_mfa(response: dict, otp_provider) -> dict:
    if otp_provider is None:
        raise ProbeError("mfa_required", "此账号需要二次验证，请通过支持验证码输入的本机登录流程完成。")
    pending = response.get("mfaPendingToken")
    methods = response.get("mfaMethods")
    if not isinstance(pending, str) or not pending or not isinstance(methods, list):
        raise ProbeError("invalid_response", "OpenReview 未返回有效的二次验证流程。")
    supported = [method for method in ("totp", "emailOtp") if method in methods]
    if not supported:
        raise ProbeError("mfa_required", "此账号需要本工具尚不支持的二次验证方式，请使用 OpenReview 官方登录界面。")
    preferred = response.get("preferredMethod")
    method = preferred if preferred in supported else supported[0]
    if method == "emailOtp":
        _post("/mfa/challenge", {"mfaPendingToken": pending, "method": method})
    try:
        code = otp_provider(method)
    except Exception:
        raise ProbeError("mfa_cancelled", "二次验证输入未完成。") from None
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6,8}", code.strip()):
        raise ProbeError("mfa_required", "请提供有效的二次验证码后重新登录。")
    try:
        return _post("/mfa/verify", {"mfaPendingToken": pending, "method": method, "code": code.strip()})
    except ProbeError as error:
        if error.code == "unauthorized":
            raise ProbeError("mfa_failed", "二次验证未通过，请检查验证码后重新登录。") from None
        raise


def _post(path: str, payload: dict) -> dict:
    request = Request(
        API_BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json", "User-Agent": "iclr-alert-verification/0.1"},
        method="POST",
    )
    return _request_json(request, service="openreview")
