"""Loopback-only setup UI. Not a cloud deployment or a polling service."""

import json
import os
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import ProbeError, probe_openreview, send_bark_test

PROJECT_DIR = Path(__file__).resolve().parent.parent
PRIVATE_DIR = Path(os.environ.get("ICLR_CONFIG_DIR", str(PROJECT_DIR / ".local"))).expanduser()


def read_json(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class SetupState:
    def __init__(self, paper):
        self.config = read_json(PRIVATE_DIR / "config.json")
        self.config.setdefault("paper", paper)
        self.status = read_json(PRIVATE_DIR / "status.json")
        self.status.setdefault("openreview", {"status": "not_checked"})
        self.status.setdefault("bark", {"status": "not_sent"})
        self.lock = threading.Lock()

    def public_status(self):
        return {
            **self.status,
            "paper": self.config["paper"],
            "bark_configured": bool(self.config.get("bark_key")),
            "token_configured": bool(self.config.get("openreview_token")),
        }

    def save(self):
        write_json(PRIVATE_DIR / "config.json", self.config)
        write_json(PRIVATE_DIR / "status.json", self.status)

    def probe(self, body):
        paper = body.get("paper", "").strip()
        token = body.get("token", "").strip()
        self.config.update(paper=paper, openreview_token=token)
        try:
            self.status["openreview"] = probe_openreview(paper, token)
        except ProbeError as error:
            self.status["openreview"] = {"status": "error", "code": error.code, "message": str(error)}
        self.save()

    def bark(self, body):
        previous = self.status["bark"]
        if previous.get("accepted_at"):
            elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(previous["accepted_at"].replace("Z", "+00:00"))
            if elapsed.total_seconds() < 60:
                raise ProbeError("cooldown", "刚发送过测试，请先查看手机；60 秒后可再次发送。")
        device_key = body.get("device_key", "").strip() or self.config.get("bark_key", "")
        # Store only after the transport has accepted the configured key.
        result = send_bark_test(device_key)
        self.config["bark_key"] = device_key
        self.status["bark"] = result
        self.save()

    def confirm(self, body):
        result = self.status["bark"]
        if result.get("status") not in {"accepted", "confirmed"} or not secrets.compare_digest(
            str(body.get("test_id", "")), str(result.get("test_id", "missing"))
        ):
            raise ProbeError("no_test", "请先发送测试，再确认收到对应的测试编号。")
        result.update(status="confirmed", confirmed_at=datetime.now(timezone.utc).isoformat())
        self.save()


def serve_local(port, paper):
    state = SetupState(paper)
    session_token = secrets.token_urlsafe(32)
    origin = f"http://127.0.0.1:{port}"
    page_path = f"/setup/{session_token}"
    html_template = Path(__file__).with_name("setup.html").read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never put setup paths, credentials, or request bodies in logs.

        def send_content(self, code, content, content_type="application/json; charset=utf-8"):
            payload = content.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{port}"

        def authorized(self):
            return self.valid_host() and secrets.compare_digest(self.headers.get("X-Setup-Token", ""), session_token)

        def reply_status(self):
            self.send_content(200, json.dumps(state.public_status(), ensure_ascii=False))

        def do_GET(self):
            if not self.valid_host():
                self.send_content(403, '{}')
                return
            if self.path == page_path:
                self.send_content(200, html_template.replace("__SESSION_TOKEN__", session_token), "text/html; charset=utf-8")
                return
            if self.path == "/api/status" and self.authorized():
                with state.lock:
                    self.reply_status()
                return
            self.send_content(404, '{}')

        def do_POST(self):
            if not self.authorized() or self.headers.get("Origin") != origin:
                self.send_content(403, json.dumps({"message": "请从本机配置页操作。"}, ensure_ascii=False))
                return
            actions = {"/api/probe": state.probe, "/api/bark": state.bark, "/api/confirm": state.confirm}
            if self.path not in actions:
                self.send_content(404, '{}')
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16_384:
                    raise ValueError()
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict) or any(not isinstance(value, str) for value in body.values()):
                    raise ValueError()
                with state.lock:
                    actions[self.path](body)
                    self.reply_status()
            except ProbeError as error:
                self.send_content(400, json.dumps({"code": error.code, "message": str(error)}, ensure_ascii=False))
            except (ValueError, UnicodeError):
                self.send_content(400, json.dumps({"message": "输入格式不正确。"}, ensure_ascii=False))
            except Exception:
                # Never echo an exception that might contain request credentials.
                self.send_content(500, json.dumps({"message": "本机验证失败，请检查项目和网络。"}, ensure_ascii=False))

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    write_json(PRIVATE_DIR / "launch.json", {"url": origin + page_path})
    print(f"Local setup: {origin + page_path}", flush=True)
    print("Only this computer can access it. Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
