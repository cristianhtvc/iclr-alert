"""Single-process polling, bounded backoff and non-repeating health alerts."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
import time

from .core import ProbeError, parse_forum_id
from .monitor import Monitor, send_notification
from .settings import require_paper, settings
from .web import read_json, write_json

STOP_ACTIONS = {"needs_auth", "access_verification"}


def run(watch=False, *, sleep=time.sleep):
    config = settings()
    config["paper"] = require_paper(config)
    if not config["bark_key"]:
        raise ProbeError("invalid_bark_key", "请先配置 Bark Device Key。")
    config["data_dir"].mkdir(parents=True, exist_ok=True)
    status_path = config["data_dir"] / "monitor-status.json"
    with worker_lock(config["data_dir"] / "monitor.lock"):
        monitor = Monitor(config["db_path"])
        failure_count = 0
        while True:
            config = settings()  # A refreshed local session takes effect on the next check.
            config["paper"] = require_paper(config)
            result = monitor.check_once(
                config["paper"], config["token"], config["bark_key"],
                waiting_interval_seconds=config["waiting_interval_seconds"],
                waiting_message=config["waiting_message"], update_message=config["update_message"],
            )
            previous = read_json(status_path)
            if watch and result.get("action") in STOP_ACTIONS:
                result["health_alert"] = _health_alert(config, result, previous)
            result["updated_at"] = datetime.now(timezone.utc).isoformat()
            write_json(status_path, result)
            # Logs contain outcome/counts only, not credentials, requests or review prose.
            probe = result.get("probe", {})
            print(f"{result['updated_at']} status={result['status']} code={result.get('code', '-')} push={result['notification_status']} kind={result.get('notification_kind', '-')} reviews={probe.get('review_count', '-')} scored={probe.get('scored_review_count', '-')}", flush=True)
            if not watch or result.get("action") in STOP_ACTIONS:
                return result
            # A Bark failure must not slow otherwise healthy OpenReview checks.
            read_failed = result["status"] == "error" and result.get("action") != "retry_push"
            failure_count = failure_count + 1 if read_failed else 0
            delay = next_delay(config["interval"], failure_count, result.get("retry_after"))
            sleep(delay)


def next_delay(interval, failures, retry_after=None):
    delay = max(interval, min(1800, interval * (2 ** min(failures, 6))))
    return max(delay, retry_after or 0)


def _health_alert(config, result, previous):
    if previous.get("code") == result["code"] and previous.get("health_alert") == "accepted":
        return "accepted"
    forum_id = parse_forum_id(config["paper"])
    event_id = hashlib.sha256((forum_id + ":" + result["code"]).encode()).hexdigest()[:20]
    event = {
        "id": "iclr-health-" + event_id,
        "title": "ICLR 监控需要处理",
        "body": str(result["message"]) + "\n评分检查已暂停；完成登录或官方访问验证后请重启监控。",
        "group": "iclr-monitor-status",
        "url": "https://openreview.net/forum?id=" + forum_id,
    }
    try:
        send_notification(config["bark_key"], event)
    except ProbeError:
        return "pending"
    return "accepted"


@contextmanager
def worker_lock(path):
    with open(path, "a+b") as lock:
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ProbeError("already_running", "已有监控进程使用此状态目录，请勿重复启动。") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)
