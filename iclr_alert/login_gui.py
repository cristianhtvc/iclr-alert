"""A user-operated local login window. Never persist a password or OTP."""

import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import simpledialog, ttk

from .auth import login
from .core import ProbeError, probe_openreview
from .settings import require_paper, save_session, settings
from .web import PRIVATE_DIR, write_json


def main():
    from .__main__ import require_windows_login
    require_windows_login()
    paper = require_paper(settings())
    root = tk.Tk()
    root.title("ICLR 提醒 · OpenReview 正常登录")
    root.geometry("680x500")
    root.minsize(600, 460)
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="连接你的 OpenReview 账号", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
    ttk.Label(frame, text="凭据只提交给 https://api2.openreview.net 的官方登录接口。\n密码和验证码不保存、不写入聊天。会话使用 Windows 加密保护。", wraplength=620).pack(anchor="w", pady=(12, 16))
    ttk.Label(frame, text="OpenReview 邮箱 / 用户名").pack(anchor="w")
    username = ttk.Entry(frame, width=65)
    username.pack(fill="x", pady=(4, 12))
    ttk.Label(frame, text="密码").pack(anchor="w")
    password = ttk.Entry(frame, show="●", width=65)
    password.pack(fill="x", pady=(4, 14))
    message = tk.StringVar(value="填写后点击登录。登录成功后会立即检查目标论文 API，不发送评分通知。")
    verification_url = {"value": None}
    closed = threading.Event()

    def set_status(result, text):
        write_json(PRIVATE_DIR / "auth-status.json", result)
        message.set(text)

    def ask_otp(method):
        response = queue.Queue(maxsize=1)

        def prompt():
            code = simpledialog.askstring("OpenReview 验证码", "请输入邮箱验证码：" if method == "emailOtp" else "请输入验证器的一次性验证码：", show="●", parent=root)
            response.put(code or "")

        if closed.is_set():
            return ""
        root.after(0, prompt)
        try:
            return response.get(timeout=300)
        except queue.Empty:
            return ""

    def finish(result, text):
        if not closed.is_set():
            set_status(result, text)
            submit.configure(state="normal")

    def work(account, secret):
        try:
            session = login(account, secret, otp_provider=ask_otp)
            save_session(session["token"])
            probe = probe_openreview(paper, session["token"])
            result = {"status": "verified", "probe": probe}
            text = f"登录成功，论文 API 已正常返回。\n当前可见正式评审：{probe['review_count']} 份，含数值评分：{probe['scored_review_count']} 份。\n会话已加密保存，未保存密码。可以关闭此窗口。"
        except ProbeError as error:
            verification_url["value"] = error.verification_url
            result = {"status": "error", "code": error.code, "message": str(error)}
            if error.verification_url:
                result["verification_url"] = error.verification_url
            text = str(error)
            if error.verification_url:
                text += "\n可点击下方按钮打开官方验证页面并手动处理；完成网页验证不代表程序已自动恢复。"
        except Exception:
            result = {"status": "error", "code": "local_login_error", "message": "本机登录未完成，请检查网络或重试。"}
            text = result["message"]
        finally:
            secret = ""
        if not closed.is_set():
            root.after(0, lambda: finish(result, text))

    def start():
        account, secret = username.get().strip(), password.get()
        if not account or not secret:
            message.set("请填写用户名和密码。")
            return
        password.delete(0, "end")
        submit.configure(state="disabled")
        set_status({"status": "running"}, "正在通过官方接口登录并验证论文 API……")
        threading.Thread(target=work, args=(account, secret), daemon=True).start()

    submit = ttk.Button(frame, text="登录并验证论文 API", command=start)
    submit.pack(anchor="w")
    ttk.Label(frame, textvariable=message, wraplength=620, justify="left").pack(anchor="w", pady=16)
    ttk.Button(frame, text="打开官方访问验证页（仅在提示需要时）", command=lambda: webbrowser.open(verification_url["value"]) if verification_url["value"] else None).pack(anchor="w")
    ttk.Label(frame, text="不会读取浏览器 Cookie，也不会修改你的投稿。", foreground="#56616c").pack(anchor="w", pady=12)
    root.protocol("WM_DELETE_WINDOW", lambda: (closed.set(), root.destroy()))
    username.focus_set()
    write_json(PRIVATE_DIR / "auth-status.json", {"status": "waiting_for_user"})
    root.mainloop()


if __name__ == "__main__":
    main()
