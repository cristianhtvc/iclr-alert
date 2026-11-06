"""Single-worker personal monitor with durable snapshots and a push outbox."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlencode
from urllib.request import Request
from uuid import uuid4

from . import core
from .core import ProbeError, probe_openreview


DEFAULT_WAITING_INTERVAL_SECONDS = 3600
DEFAULT_WAITING_MESSAGE = "继续等待，不要着急，不要担心，你是最棒的！"
DEFAULT_UPDATE_MESSAGE = "！！！！！！！！！！重要警报！！！！！！你的ICLR分数出来了，快来看咋样"


class Monitor:
    """Use one monitor process per database; accepted pushes are not delivery receipts."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS paper_state (
                    forum_id TEXT PRIMARY KEY,
                    reviews_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY,
                    forum_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    accepted_at TEXT
                );
                CREATE TABLE IF NOT EXISTS waiting_schedule (
                    forum_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    pending_json TEXT
                );
            """)

    def check_once(
        self, paper: str, token: str, bark_key: str, *,
        waiting_interval_seconds=DEFAULT_WAITING_INTERVAL_SECONDS,
        waiting_message=DEFAULT_WAITING_MESSAGE,
        update_message=DEFAULT_UPDATE_MESSAGE,
    ) -> dict:
        try:
            forum_id = core.parse_forum_id(paper)
            probe = probe_openreview(paper, token)
        except ProbeError as error:
            return _failure(error)
        try:
            with closing(self._connect()) as db:
                changed = self._store_observation(
                    db, probe, waiting_interval_seconds=waiting_interval_seconds,
                    waiting_message=waiting_message, update_message=update_message,
                )
                result = {
                    "status": "ok", "data_changed": changed,
                    "notification_status": "none", "probe": probe,
                }
                # One attempt per check. A timeout can still have reached the provider;
                # retries reuse the stored event ID, without claiming exactly-once delivery.
                pending = db.execute(
                    "SELECT event_id, payload_json FROM outbox WHERE forum_id=? "
                    "AND accepted_at IS NULL ORDER BY created_at, rowid LIMIT 1", (forum_id,),
                ).fetchone()
                kind = "update"
                if pending is None:
                    waiting = db.execute(
                        "SELECT pending_json FROM waiting_schedule WHERE forum_id=?", (forum_id,),
                    ).fetchone()
                    if waiting and waiting[0]:
                        payload = json.loads(waiting[0])
                        pending = payload["id"], waiting[0]
                        kind = "waiting"
                if pending:
                    event_id, payload_json = pending
                    result["notification_kind"] = kind
                    try:
                        send_notification(bark_key, json.loads(payload_json))
                    except ProbeError as error:
                        result.update(_failure(error, changed=changed))
                        result["notification_status"] = "pending"
                        result["action"] = "retry_push"
                    else:
                        with db:
                            if kind == "update":
                                db.execute("UPDATE outbox SET accepted_at=? WHERE event_id=?", (_now(), event_id))
                            else:
                                db.execute(
                                    "UPDATE waiting_schedule SET started_at=?,pending_json=NULL WHERE forum_id=?",
                                    (_now(), forum_id),
                                )
                        result.update(notification_status="accepted", event_id=event_id)
                result["pending_count"] = db.execute(
                    "SELECT COUNT(*) FROM outbox WHERE forum_id=? AND accepted_at IS NULL", (forum_id,),
                ).fetchone()[0]
                result["pending_count"] += db.execute(
                    "SELECT COUNT(*) FROM waiting_schedule WHERE forum_id=? AND pending_json IS NOT NULL", (forum_id,),
                ).fetchone()[0]
                return result
        except sqlite3.Error:
            return _failure(ProbeError("storage_error", "监控状态无法保存，请检查持久化磁盘。"))

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def _store_observation(self, db, probe, *, waiting_interval_seconds, waiting_message, update_message):
        forum_id = probe["forum_id"]
        row = db.execute("SELECT reviews_json FROM paper_state WHERE forum_id=?", (forum_id,)).fetchone()
        previous = json.loads(row[0]) if row else {}
        merged, added, modified = _merge_reviews(previous, probe["reviews"])
        changed = bool(added or modified)
        now = _now()
        with db:
            db.execute(
                "INSERT INTO paper_state(forum_id,reviews_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(forum_id) DO UPDATE SET reviews_json=excluded.reviews_json,updated_at=excluded.updated_at",
                (forum_id, json.dumps(merged, ensure_ascii=False), now),
            )
            if changed:
                event = _notification(
                    probe, first=not previous, added=bool(added), modified=bool(modified),
                    update_message=update_message,
                )
                db.execute(
                    "INSERT INTO outbox(event_id,forum_id,payload_json,created_at) VALUES(?,?,?,?)",
                    (event["id"], forum_id, json.dumps(event, ensure_ascii=False), now),
                )
            _schedule_waiting(
                db, probe, changed=changed, now=now,
                interval=waiting_interval_seconds, message=waiting_message,
            )
        return changed


def _schedule_waiting(db, probe, *, changed, now, interval, message):
    """Persist one waiting event at most; only successful observations reach here."""
    forum_id = probe["forum_id"]
    row = db.execute(
        "SELECT started_at,pending_json FROM waiting_schedule WHERE forum_id=?", (forum_id,),
    ).fetchone()
    if changed or row is None or interval == 0:
        db.execute(
            "INSERT INTO waiting_schedule(forum_id,started_at,pending_json) VALUES(?,?,NULL) "
            "ON CONFLICT(forum_id) DO UPDATE SET started_at=excluded.started_at,pending_json=NULL",
            (forum_id, now),
        )
        return
    if row[1]:
        return
    # Do not create encouragement while a score alert still needs delivery.
    if db.execute(
        "SELECT 1 FROM outbox WHERE forum_id=? AND accepted_at IS NULL LIMIT 1", (forum_id,),
    ).fetchone():
        return
    elapsed = datetime.fromisoformat(now) - datetime.fromisoformat(row[0])
    if elapsed.total_seconds() < interval:
        return
    event = {
        "id": "iclr-waiting-" + uuid4().hex,
        "title": "ICLR：继续等待", "body": message,
        "url": "https://openreview.net/forum?" + urlencode({"id": forum_id}),
        "group": "iclr-review-monitor",
    }
    db.execute(
        "UPDATE waiting_schedule SET pending_json=? WHERE forum_id=?",
        (json.dumps(event, ensure_ascii=False), forum_id),
    )


def _merge_reviews(previous, observed):
    # Missing notes/fields can reflect permissions or incomplete publication. Retain
    # the last known score so a temporary visibility gap cannot reset the baseline.
    merged = dict(previous)
    added, modified = [], []
    for review in observed:
        review_id = review["id"]
        if not review_id or core._numeric_rating(review.get("rating")) is None:
            continue
        prior = previous.get(review_id)
        current = dict(review)
        if prior and current.get("confidence") is None:
            current["confidence"] = prior.get("confidence")
        merged[review_id] = current
        if prior is None:
            added.append(review_id)
        elif _score_signature(prior) != _score_signature(current):
            modified.append(review_id)
    return merged, added, modified


def _score_signature(review):
    confidence = review.get("confidence")
    normalized = core._numeric_rating(confidence)
    return core._numeric_rating(review.get("rating")), normalized if normalized is not None else confidence


def _notification(probe, *, first, added, modified, update_message=DEFAULT_UPDATE_MESSAGE):
    if first:
        title = "ICLR：评审分数已可见"
    elif added and modified:
        title = "ICLR：新增评审及评分更新"
    elif added:
        title = "ICLR：新增评审分数"
    else:
        title = "ICLR：评审分数或置信度更新"
    lines = [
        update_message,
        str(probe["title"]),
        f"当前可见 {probe['review_count']} 份评审，可读评分 {probe['scored_review_count']} 份。",
    ]
    for review in sorted(probe["reviews"], key=lambda item: item["id"]):
        rating = review.get("rating")
        confidence = review.get("confidence")
        lines.append(
            f"{review.get('reviewer') or review['id']}：{rating if rating is not None else '评分未可见'}"
            f"；置信度 {confidence if confidence is not None else '未可见'}"
        )
    if probe["average_rating"] is not None:
        lines.append(f"当前可读评分均值（本程序计算）：{probe['average_rating']:g}")
    checked_at = datetime.fromisoformat(probe["checked_at"].replace("Z", "+00:00"))
    local_time = checked_at.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    lines.append("检测时间（北京时间）：" + local_time)
    return {
        "id": "iclr-" + uuid4().hex,
        "title": title,
        "body": "\n".join(lines),
        "url": "https://openreview.net/forum?" + urlencode({"id": probe["forum_id"]}),
        "group": "iclr-review-monitor",
    }


def send_notification(device_key: str, event: dict) -> None:
    key = device_key.strip()
    if not re.fullmatch(core.BARK_KEY_PATTERN, key):
        raise ProbeError("invalid_bark_key", "Bark 设备密钥未配置或格式不正确。")
    payload = {**event, "device_key": key}
    request = Request(
        core.BARK_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "iclr-alert-monitor/0.1"},
        method="POST",
    )
    response = core._request_json(request, service="bark")
    if response.get("code") != 200:
        raise ProbeError("bark_rejected", "Bark 未接受评分通知；通知已留在本地待发送队列。")


def _failure(error, *, changed=False):
    action = "retry"
    if error.code in {"unauthorized", "forbidden"}:
        action = "needs_auth"
    elif error.code in {"challenge_required", "access_verification"}:
        action = "access_verification"
    result = {
        "status": "error", "data_changed": changed, "notification_status": "none",
        "code": error.code, "message": str(error), "action": action,
    }
    if error.verification_url:
        result["verification_url"] = error.verification_url
    if error.retry_after is not None:
        result["retry_after"] = error.retry_after
    return result


def _now():
    return datetime.now(timezone.utc).isoformat()
