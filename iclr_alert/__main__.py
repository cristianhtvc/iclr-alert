"""Run with python -m iclr_alert from the project folder."""

import argparse
import getpass
import json
import sys

from .core import ProbeError, parse_forum_id, probe_openreview


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="OpenReview review monitoring with Bark notifications")
    subparsers = parser.add_subparsers(dest="command", required=True)
    paper = subparsers.add_parser("paper", help="Configure the OpenReview paper URL or forum ID to monitor")
    paper.add_argument("paper")
    probe = subparsers.add_parser("probe", help="Read reviews using the configured normal API session")
    probe.add_argument("--paper")
    probe.add_argument("--anonymous", action="store_true")
    subparsers.add_parser("login", help="Normal API login; password and OTP are hidden and never saved")
    subparsers.add_parser("login-gui", help="Open the local interactive login window")
    subparsers.add_parser("bark-key", help="Set the original Device Key shown in the Bark app")
    subparsers.add_parser("bark-test", help="Send one explicit test to the configured Bark device")
    subparsers.add_parser("check", help="Check once and notify if scores changed")
    subparsers.add_parser("watch", help="Poll continuously; stop on auth or access-verification errors")
    subparsers.add_parser("status", help="Show safe configuration flags and monitoring status")
    subparsers.add_parser("cloud-config", help="Prepare a private Railway variables file; never upload or print secrets")
    serve = subparsers.add_parser("serve", help="Open the local configuration service")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--paper")
    args = parser.parse_args()
    try:
        return execute(args)
    except ProbeError as error:
        result = {"status": "error", "code": error.code, "message": str(error)}
        if error.verification_url:
            result["verification_url"] = error.verification_url
        output(result)
        return 1
    except KeyboardInterrupt:
        print("监控已停止。")
        return 0
    except (OSError, ValueError):
        output({"status": "error", "code": "local_config_error", "message": "配置或状态文件无法读取，请检查本机文件和持久化目录。"})
        return 1


def execute(args):
    from .settings import require_paper, settings, save_session
    from .web import PRIVATE_DIR, read_json, write_json
    if args.command == "paper":
        link = "https://openreview.net/forum?id=" + parse_forum_id(args.paper)
        local = read_json(PRIVATE_DIR / "config.json")
        if not isinstance(local, dict):
            raise ProbeError("invalid_config", "本机配置格式不正确。")
        local["paper"] = link
        write_json(PRIVATE_DIR / "config.json", local)
        output({"status": "configured", "paper": link})
        return 0
    if args.command == "serve":
        from .web import serve_local
        serve_local(args.port, args.paper or settings()["paper"])
        return 0
    if args.command == "login-gui":
        require_windows_login()
        require_paper(settings())
        from .login_gui import main as login_window
        login_window()
        return 0
    if args.command == "login":
        require_windows_login()
        paper = require_paper(settings())
        from .auth import login
        username = input("OpenReview 邮箱 / 用户名：").strip()
        password = getpass.getpass("密码（不回显、不保存）：")
        session = login(username, password, otp_provider=lambda method: getpass.getpass(f"{method} 一次性验证码："))
        password = ""
        save_session(session["token"])
        output({"status": "authenticated", "message": "会话已在本机加密保存，正在检查论文 API。"})
        output(probe_openreview(paper, session["token"]))
        return 0
    if args.command == "bark-key":
        from .core import BARK_KEY_PATTERN
        import re
        key = getpass.getpass("Bark App 推送地址中的 Device Key（不回显）：").strip()
        if not re.fullmatch(BARK_KEY_PATTERN, key):
            raise ProbeError("invalid_bark_key", "Device Key 格式不正确。")
        local = read_json(PRIVATE_DIR / "config.json")
        local.update(bark_key=key, bark_key_source="app_push_url")
        write_json(PRIVATE_DIR / "config.json", local)
        output({"status": "configured", "message": "已保存 App 原有推送 Key，未发送通知。"})
        return 0
    config = settings()
    if args.command == "bark-test":
        from .core import send_bark_test
        output(send_bark_test(config["bark_key"]))
        return 0
    if args.command == "status":
        output({"bark_configured": bool(config["bark_key"]), "authenticated": bool(config["token"]), "paper": config["paper"], "interval_seconds": config["interval"], "monitor": read_json(config["data_dir"] / "monitor-status.json"), "login": read_json(PRIVATE_DIR / "auth-status.json")})
        return 0
    if args.command == "probe":
        paper = require_paper({"paper": args.paper or config["paper"]})
        output(probe_openreview(paper, "" if args.anonymous else config["token"]))
        return 0
    if args.command == "cloud-config":
        config["paper"] = require_paper(config)
        if not config["token"] or not config["bark_key"]:
            raise ProbeError("missing_credentials", "请先完成 OpenReview 登录和 Bark 配置。")
        target = PRIVATE_DIR / "railway-variables.json"
        write_json(target, {
            "OPENREVIEW_PAPER": config["paper"], "OPENREVIEW_TOKEN": config["token"],
            "BARK_DEVICE_KEY": config["bark_key"], "CHECK_INTERVAL_SECONDS": str(config["interval"]), "DATA_DIR": "/data",
        })
        output({"status": "prepared", "path": str(target), "uploaded": False, "message": "私有变量文件已生成，未输出或上传凭据；不要加入源码或压缩包。"})
        return 0
    from .runner import run
    result = run(watch=args.command == "watch")
    if args.command == "check":
        output(result)
    # Expected human-action pauses should not trigger a cloud crash-restart loop.
    return 0 if result["status"] == "ok" or (args.command == "watch" and result.get("action") in {"needs_auth", "access_verification"}) else 2


def require_windows_login():
    if sys.platform != "win32":
        raise ProbeError("unsupported_session_store", "交互式加密登录目前支持 Windows；其他系统请通过 OPENREVIEW_TOKEN 环境变量提供合法会话。")


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
