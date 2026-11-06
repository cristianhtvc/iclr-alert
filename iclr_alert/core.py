"""Read-only OpenReview probes and a single, explicit Bark test push."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

API_BASE = "https://api2.openreview.net"
BARK_ENDPOINT = "https://api.day.app/push"
BARK_REGISTER_ENDPOINT = "https://api.day.app/register"
BARK_KEY_PATTERN = r"[A-Za-z0-9_-]{8,256}"
REQUEST_TIMEOUT_SECONDS = 20
PAGE_SIZE = 1000
MAX_FORUM_PAGES = 20
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ProbeError(Exception):
    """An actionable error whose message contains no credentials or raw response."""

    def __init__(self, code: str, message: str, verification_url: str | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.verification_url = verification_url
        self.retry_after = retry_after


def parse_forum_id(text: str) -> str:
    text = text.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", text):
        return text
    try:
        url = urlsplit(text)
        ids = parse_qs(url.query).get("id", [])
        valid_url = (
            url.scheme == "https"
            and url.hostname in {"openreview.net", "www.openreview.net"}
            and url.path.rstrip("/") == "/forum"
            and url.username is None
            and url.password is None
            and url.port in {None, 443}
        )
    except ValueError:
        valid_url, ids = False, []
    if valid_url and len(ids) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ids[0]):
        return ids[0]
    raise ProbeError("invalid_paper", "请输入 OpenReview 论文链接或有效的 forum ID。")


def probe_openreview(paper: str, token: str = "") -> dict:
    forum_id = parse_forum_id(paper)
    headers = {"Accept": "application/json", "User-Agent": "iclr-alert-verification/0.1"}
    if token.strip():
        headers["Authorization"] = "Bearer " + token.strip().removeprefix("Bearer ")
    submission_payload = _get_notes({"id": forum_id}, headers)
    submissions = submission_payload["notes"]
    if len(submissions) != 1 or submissions[0].get("id") != forum_id:
        raise ProbeError("not_found", "当前身份无法读取该论文，请核对链接和访问权限。")
    reviews = [_summarize_review(note) for note in _get_forum_notes(forum_id, headers) if _is_review(note)]
    reviews.sort(key=lambda review: review["id"])
    numeric = [score for review in reviews if (score := _numeric_rating(review["rating"])) is not None]
    average = sum(numeric) / len(numeric) if numeric else None
    status = "reviews_visible" if numeric else "ratings_unavailable" if reviews else "no_visible_reviews"
    return {
        "status": status,
        "forum_id": forum_id,
        "title": _content_value(submissions[0].get("content", {}), "title") or "未提供论文标题",
        "review_count": len(reviews),
        "scored_review_count": len(numeric),
        "reviews": reviews,
        "average_rating": round(average, 4) if average is not None else None,
        "checked_at": _utc_now(),
        "source_url": "https://openreview.net/forum?" + urlencode({"id": forum_id}),
    }


def register_bark_device(device_token: str) -> str:
    token = device_token.strip()
    if not re.fullmatch(r"[A-Fa-f0-9]{16,160}", token):
        raise ProbeError("invalid_device_token", "请输入有效的 iPhone Bark 设备 Token（16–160 位十六进制字符）。")
    request = Request(
        BARK_REGISTER_ENDPOINT,
        data=json.dumps({"device_token": token, "device_type": "ios"}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "iclr-alert-verification/0.1"},
        method="POST",
    )
    result = _request_json(request, service="bark")
    data = result.get("data")
    key = (data.get("device_key") or data.get("key")) if isinstance(data, dict) else None
    if result.get("code") != 200 or not isinstance(key, str) or not re.fullmatch(BARK_KEY_PATTERN, key):
        raise ProbeError("bark_registration_failed", "Bark 未返回有效设备密钥，请检查设备 Token 与 App 配置。")
    return key


def send_bark_test(device_key: str) -> dict:
    key = device_key.strip()
    if not re.fullmatch(BARK_KEY_PATTERN, key):
        raise ProbeError("invalid_bark_key", "请填写 Bark 设备密钥本身，不要粘贴完整推送网址。")
    test_id = uuid4().hex[:12]
    payload = {
        "device_key": key,
        "title": "ICLR 提醒：推送测试",
        "body": f"这是一条连通性测试，不是真实出分通知。测试编号：{test_id}。请在本机配置页确认收到。",
        "group": "iclr-api-verification",
        "ttl": 600,
    }
    request = Request(
        BARK_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "iclr-alert-verification/0.1"},
        method="POST",
    )
    result = _request_json(request, service="bark")
    if result.get("code") != 200:
        raise ProbeError("bark_rejected", "Bark 服务未接受测试通知，请核对设备密钥和 App 配置。")
    return {"status": "accepted", "test_id": test_id, "accepted_at": _utc_now()}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Neither the OpenReview authorization header nor a push body may follow redirects.
        return None


def urlopen(request, *, timeout):
    """A patchable transport seam with redirects disabled and no automatic retries."""
    return build_opener(_NoRedirect).open(request, timeout=timeout)


def _request_json(request: Request, *, service: str) -> dict:
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise ProbeError("invalid_response", "服务返回了非预期的响应状态。")
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProbeError("invalid_response", "服务响应过大，请缩小查询范围。")
        payload = json.loads(body)
    except HTTPError as error:
        if service == "openreview":
            try:
                error_payload = json.loads(error.read(8192))
            except (ValueError, UnicodeError, OSError):
                error_payload = {}
            if isinstance(error_payload, dict) and error_payload.get("name") == "ChallengeRequiredError":
                verification_url = "https://openreview.net/challenge?" + urlencode({"redirect": request.full_url})
                raise ProbeError("challenge_required", "OpenReview 要求人工访问验证；尚未取得论文 API 数据，不能判断是否出分或是否有读取权限。", verification_url) from None
        if service == "openreview" and "html" in error.headers.get("Content-Type", "").lower():
            raise ProbeError("access_verification", "OpenReview 返回了网页访问验证，未取得论文 API 数据；请先在浏览器确认能否正常访问。") from None
        safe_error = _http_error(error.code, service)
        if error.code == 429:
            raw_delay = error.headers.get("Retry-After", "")
            try:
                safe_error.retry_after = max(0, int(raw_delay))
            except ValueError:
                try:
                    retry_date = parsedate_to_datetime(raw_delay)
                    safe_error.retry_after = max(0, (retry_date - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        raise safe_error from None
    except (URLError, TimeoutError, OSError):
        raise ProbeError("network_error", "网络连接失败或超时，请稍后重试。") from None
    except (ValueError, UnicodeError):
        raise ProbeError("invalid_response", "服务未返回有效的 JSON 数据。") from None
    if not isinstance(payload, dict):
        raise ProbeError("invalid_response", "服务返回的数据结构不符合预期。")
    return payload


def _http_error(status: int, service: str) -> ProbeError:
    if service == "bark":
        return ProbeError("bark_rejected", "Bark 请求未成功。请检查密钥；若发生超时，请先检查手机，避免重复发送。")
    errors = {
        401: ("unauthorized", "OpenReview 认证无效或已失效，请更新会话后重试。"),
        403: ("forbidden", "OpenReview 拒绝了请求；需核对访问验证及当前身份权限。"),
        404: ("not_found", "当前身份无法读取该论文，请核对链接和访问权限。"),
        429: ("rate_limited", "OpenReview 限流，请等待后再检查。"),
    }
    code, message = errors.get(status, ("service_error", "OpenReview 服务暂时不可用，请稍后重试。"))
    return ProbeError(code, message)


def _get_notes(filters: dict, headers: dict) -> dict:
    request = Request(API_BASE + "/notes?" + urlencode(filters), headers=headers, method="GET")
    payload = _request_json(request, service="openreview")
    notes = payload.get("notes")
    if not isinstance(notes, list) or any(not isinstance(note, dict) for note in notes):
        raise ProbeError("invalid_response", "OpenReview 未返回有效的 notes 列表。")
    return payload


def _get_forum_notes(forum_id: str, headers: dict) -> list:
    notes = []
    for _ in range(MAX_FORUM_PAGES):
        offset = len(notes)
        payload = _get_notes({"forum": forum_id, "limit": PAGE_SIZE, "offset": offset}, headers)
        batch = payload["notes"]
        notes.extend(batch)
        count = payload.get("count")
        if not batch or (isinstance(count, int) and len(notes) >= count):
            return notes
        if not isinstance(count, int) and len(batch) < PAGE_SIZE:
            return notes
    raise ProbeError("too_many_notes", "该论文讨论数量超出验证工具范围，请缩小查询范围。")


def _is_review(note: dict) -> bool:
    invitations = note.get("invitations", [])
    return isinstance(invitations, list) and any(
        isinstance(invitation, str) and invitation.endswith("/-/Official_Review") for invitation in invitations
    )


def _content_value(content: dict, field: str):
    if not isinstance(content, dict):
        return None
    value = content.get(field)
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str):
        return value.strip() or None
    return value if math.isfinite(value) else None


def _summarize_review(note: dict) -> dict:
    signatures = note.get("signatures", [])
    reviewer = signatures[0].rsplit("/", 1)[-1] if isinstance(signatures, list) and signatures and isinstance(signatures[0], str) else "匿名评审"
    content = note.get("content", {})
    return {
        "id": str(note.get("id", "")),
        "reviewer": reviewer,
        "rating": _content_value(content, "rating"),
        "confidence": _content_value(content, "confidence"),
    }


def _numeric_rating(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(?:\s*[:：]\s*.+)?", value.strip())
    if not match:
        return None
    number = float(match.group(1))
    return number if math.isfinite(number) else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
