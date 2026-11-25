#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iCloudx 下载工具 —— icloudpd 的简易图形启动器。

1. 填写 iCloud 账号、密码、选区、保存目录、自定义参数
2. 使用 .app 同级目录下对应系统的 icloudpd 可执行文件（不打包，方便独立换版本）
3. 默认参数来自配置文件（界面不显示，可在「调试查看」里看）
4. 构建完整命令并运行，实时显示且自动保存日志
5. 运行中出现双重认证提示时，在界面上输入验证码回传给子进程
6. 配置文件导入/导出；配置按用户名标识保存副本
"""

import json
import math
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext

IS_FROZEN = bool(getattr(sys, "frozen", False))

if IS_FROZEN:
    exe_path = Path(sys.executable).resolve()
    BUNDLE_DIR = exe_path.parent
    if sys.platform == "darwin":
        # sys.executable = iCloudx.app/Contents/MacOS/iCloudx
        # 主程序不打包进 .app，只从 .app 的上一级目录（同级）读取
        APP_DIR = BUNDLE_DIR.parent.parent.parent
    else:
        # Windows: 配置/日志/外置主程序都放在 exe 所在目录
        APP_DIR = BUNDLE_DIR
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    APP_DIR = BUNDLE_DIR

CONFIG_PATH = APP_DIR / "iCloudx-config.json"
LOG_DIR = APP_DIR / "iCloudx-logs"
DEFAULT_ARGS = "--live-photo-mov-filename-policy original --log-level info --no-progress-bar"
SUCCESS_MARKER = "All photos and videos have been downloaded"

BIN_NAME = "iCloudpd-Win.exe" if os.name == "nt" else "iCloudpd-Mac"
DEFAULT_CONFIG = {
    "domain": "cn",
    "username": "",
    "default_args": DEFAULT_ARGS,
    "custom_args": "",
    "directory": "",
    "binary_path": "",
    "remember_password": False,
    "password": "",
}

# Material Design 风格：浅灰底、白色圆角卡片、紫色主色
APP_BG = "#FAFAFC"
CARD_BG = "#FFFFFF"
SURFACE = "#F3EDFF"        # 浅紫输入底
SURFACE_HOVER = "#EAE0FF"
TEXT = "#1C1B1F"
MUTED = "#6F6B78"
ACCENT = "#6750A4"         # Material 3 primary
ACCENT_HOVER = "#533E8E"
TONAL = "#EADDFF"
SHADOW = "#E9E5F0"

if sys.platform == "darwin":
    FONT_TITLE = ("SF Pro Display", 25, "bold")
    FONT_HEADING = ("SF Pro Text", 11, "bold")
    FONT_BODY = ("SF Pro Text", 12)
    FONT_SMALL = ("SF Pro Text", 11)
    FONT_MONO = ("SF Mono", 11)
else:
    FONT_TITLE = ("Segoe UI", 22, "bold")
    FONT_HEADING = ("Segoe UI", 10, "bold")
    FONT_BODY = ("Segoe UI", 10)
    FONT_SMALL = ("Segoe UI", 9)
    FONT_MONO = ("Consolas", 10)

# 子进程要输入 2FA 时打印的提示，匹配这些字样就打开输入区
TWO_FA_HINTS = (
    "two-factor authentication code",
    "two factor authentication code",
    "verification code",
    "device index",
    "双重认证代码",
    "验证码",
)


# ---------- 与界面无关的核心逻辑（可单独测试） ----------

def default_binary_path():
    """主程序始终放在 .app（或脚本）同级目录，便于随时替换版本。"""
    return str(APP_DIR / BIN_NAME)


def parse_args(text):
    """把参数输入框的字符串拆成参数列表，兼容 Windows 的引号写法。"""
    text = (text or "").strip()
    if not text:
        return []
    if os.name == "nt":
        parts = shlex.split(text, posix=False)
        cleaned = []
        for part in parts:
            if len(part) >= 2 and part[0] == part[-1] and part[0] in ("'", '"'):
                part = part[1:-1]
            cleaned.append(part)
        return cleaned
    return shlex.split(text)


def build_command(binary, domain, username, password, directory, default_args, custom_args):
    """按约定顺序构建完整运行指令。"""
    cmd = [binary, "--domain", domain]
    cmd += parse_args(default_args)
    cmd += ["--username", username, "--password", password, "--directory", directory]
    cmd += parse_args(custom_args)
    return cmd


def format_command(cmd, mask_password=True):
    """生成可读的命令行文本；密码默认打码，避免泄露到日志。"""
    shown = []
    i = 0
    while i < len(cmd):
        part = cmd[i]
        if mask_password and part == "--password" and i + 1 < len(cmd):
            shown += ["--password", "********"]
            i += 2
            continue
        shown.append(part)
        i += 1
    if os.name == "nt":
        return subprocess.list2cmdline(shown)
    return shlex.join(shown)


def valid_username(value):
    """邮箱，或带国际区号的手机号（如 +8613800138000）。"""
    text = (value or "").strip()
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text) or
                re.fullmatch(r"\+\d{7,15}", text))


def username_key(value):
    """把用户名转成可作文件名的标识。"""
    key = re.sub(r"[^A-Za-z0-9_.@+-]", "_", (value or "").strip())
    return (key or "icloudx")[:60]


def looks_like_2fa_prompt(text):
    low = (text or "").lower()
    return any(hint in low for hint in TWO_FA_HINTS)


def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            cfg.update({key: value for key, value in data.items() if key in DEFAULT_CONFIG})
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


class Runner(threading.Thread):
    """后台运行 icloudpd：实时读输出、自动保存日志、支持回传 2FA 输入。"""

    def __init__(self, cmd, log_path, message_queue):
        super().__init__(daemon=True)
        self.cmd = cmd
        self.log_path = log_path
        self.messages = message_queue
        self.process = None
        self._lock = threading.Lock()
        self._prompted = False
        self.succeeded = False

    def send_input(self, text):
        """把界面输入的验证码送进子进程的标准输入。"""
        with self._lock:
            proc = self.process
            if proc and proc.poll() is None and proc.stdin:
                try:
                    proc.stdin.write(text.rstrip("\r\n") + "\n")
                    proc.stdin.flush()
                    return True
                except (OSError, ValueError):
                    pass
        return False

    def stop(self):
        with self._lock:
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                except OSError:
                    pass

    def _emit(self, log_file, line):
        if not line:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        log_file.write(f"[{stamp}] {line}\n")
        log_file.flush()
        self.messages.put(("log", line))
        if not self.succeeded and SUCCESS_MARKER.lower() in line.lower():
            self.succeeded = True
            self.messages.put(("success", True))

    def run(self):
        buffer = ""
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            with open(self.log_path, "a", encoding="utf-8") as log_file:
                log_file.write(f"\n===== 运行于 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
                log_file.write(f"命令: {format_command(self.cmd)}\n")
                log_file.flush()
                with self._lock:
                    self.process = subprocess.Popen(
                        self.cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        env=env,
                        creationflags=creationflags,
                    )
                assert self.process.stdout is not None
                # input() 的提示不带换行，readline() 会卡住直到进程退出，
                # 所以逐字符读：既能在提示出现时立刻通知界面，也不漏日志。
                while True:
                    char = self.process.stdout.read(1)
                    if char == "":
                        break
                    buffer += char
                    if char in "\r\n":
                        self._emit(log_file, buffer.rstrip("\r\n"))
                        buffer = ""
                        self._prompted = False   # 换行后允许下一次输入提示
                    elif not self._prompted and looks_like_2fa_prompt(buffer):
                        # 提示不带换行，先通知界面；buffer 留到换行时整行写日志
                        self._prompted = True
                        self.messages.put(("prompt2fa", "请输入双重认证验证码，或输入 a 发送短信验证码"))
                if buffer.strip():
                    self._emit(log_file, buffer.rstrip("\r\n"))
                return_code = self.process.wait()
                log_file.write(f"进程退出，返回码: {return_code}\n")
                log_file.flush()
            self.messages.put(("done", return_code))
        except Exception as exc:  # 启动失败也要反馈到界面
            self.messages.put(("error", str(exc)))
            self.messages.put(("done", -1))


# ---------- 扁平风格的基础控件 ----------

def round_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    """用矩形 + 圆弧拼圆角矩形，避免多边形平滑插值在边缘产生毛刺。"""
    r = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    fill = kwargs.pop("fill", "")
    outline = kwargs.pop("outline", "")
    tags = kwargs.pop("tags", ())
    width = kwargs.pop("width", 1)
    # 实测 Tk create_arc：0° 右上、90° 左上、180° 左下、270° 右下
    corners = (
        (x2 - r, y1 + r, 0),    # 右上
        (x2 - r, y2 - r, 270),  # 右下
        (x1 + r, y2 - r, 180),  # 左下
        (x1 + r, y1 + r, 90),   # 左上
    )
    items = []
    common = {"tags": tags}
    if fill:
        items.append(canvas.create_rectangle(x1 + r, y1, x2 - r, y2,
                                             fill=fill, outline="", **common))
        items.append(canvas.create_rectangle(x1, y1 + r, x2, y2 - r,
                                             fill=fill, outline="", **common))
        for cx, cy, start in corners:
            items.append(canvas.create_arc(cx - r, cy - r, cx + r, cy + r,
                                           start=start, extent=90, fill=fill,
                                           outline="", **common))
    if outline:
        items.append(canvas.create_line(x1 + r, y1, x2 - r, y1,
                                        fill=outline, width=width, **common))
        items.append(canvas.create_line(x2, y1 + r, x2, y2 - r,
                                        fill=outline, width=width, **common))
        items.append(canvas.create_line(x2 - r, y2, x1 + r, y2,
                                        fill=outline, width=width, **common))
        items.append(canvas.create_line(x1, y2 - r, x1, y1 + r,
                                        fill=outline, width=width, **common))
        for cx, cy, start in corners:
            items.append(canvas.create_arc(cx - r, cy - r, cx + r, cy + r,
                                           start=start, extent=90, style="arc",
                                           outline=outline, width=width, **common))
    return items


class Card(tk.Frame):
    """Material 卡片：白色圆角 + 四周柔和阴影。"""

    def __init__(self, master, radius=16, pad=12, **kwargs):
        super().__init__(master, bg=APP_BG, bd=0, highlightthickness=0, **kwargs)
        self._radius = radius
        self._canvas = tk.Canvas(self, bg=APP_BG, highlightthickness=0, bd=0)
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = tk.Frame(self, bg=CARD_BG)
        self.body.pack(fill=tk.BOTH, expand=True, padx=pad, pady=pad)
        self.body.lift()
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event):
        canvas = self._canvas
        canvas.delete("shape")
        w, h = event.width, event.height
        if w < 4 or h < 4:
            return
        r = min(self._radius, w / 2 - 1, h / 2 - 1)
        # 两层淡阴影模拟 Material 海拔感
        round_rect(canvas, 1, 3, w - 1, h - 1, r, fill=SHADOW, tags="shape")
        round_rect(canvas, 1, 1, w - 1, h - 3, r, fill=CARD_BG, tags="shape")
        canvas.tag_lower("shape")


class PillButton(tk.Canvas):
    """Material 药丸按钮：filled / tonal / outline 三种外观和禁用态。"""

    # (文字色, 底色, hover 底色, 禁用底色, 描边色)
    COLORS = {
        "primary": ("#FFFFFF", ACCENT, ACCENT_HOVER, "#E6E0EC", ""),        # filled
        "soft": (ACCENT, TONAL, "#D8C8FA", "#EFECF2", ""),                  # tonal
        "outline": (ACCENT, CARD_BG, SURFACE, "#EFECF2", ACCENT),           # outlined
    }

    def __init__(self, master, text="", command=None, kind="soft", glyph=None,
                 width=None, height=32, font=None, **kwargs):
        bg = kwargs.pop("bg", None) or str(master.cget("bg")) or CARD_BG
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, **kwargs)
        self._text = text
        self._glyph = glyph if glyph is not None else text
        self._command = command
        self._kind = kind
        self._state = "normal"
        self._font = font or FONT_BODY
        self._height = height
        self._width = width or max(height, tkfont.Font(font=self._font).measure(self._glyph) + 22)
        self.configure(width=self._width, height=height)
        self._draw()
        self.bind("<Enter>", lambda _: self._hover(True))
        self.bind("<Leave>", lambda _: self._hover(False))
        self.bind("<Button-1>", lambda _: self._click())

    def _draw(self, hover=False):
        self.delete("all")
        fg, fill, hover_fill, disabled, outline = self.COLORS.get(self._kind, self.COLORS["soft"])
        if self._state == "disabled":
            bg_color, text_color, edge = disabled, MUTED, ""
        else:
            bg_color = hover_fill if hover else fill
            text_color, edge = fg, outline
        round_rect(self, 0.5, 0.5, self._width - 1.5, self._height - 1.5,
                   self._height / 2, fill=bg_color, outline=edge, width=1 if edge else 0)
        self.create_text(self._width / 2, self._height / 2 + 0.5,
                         text=self._glyph, font=self._font, fill=text_color)

    def _hover(self, entering):
        if self._state != "disabled":
            self._draw(entering)

    def _click(self):
        if self._state == "normal" and self._command:
            self._command()

    def configure(self, **kwargs):
        state = kwargs.pop("state", None)
        text = kwargs.pop("text", None)
        glyph = kwargs.pop("glyph", None)
        kind = kwargs.pop("kind", None)
        changed = False
        if text is not None:
            self._text = text
            self._glyph = glyph if glyph is not None else text
            changed = True
        elif glyph is not None:
            self._glyph = glyph
            changed = True
        if kind is not None:
            self._kind = kind
            changed = True
        if state is not None:
            self._state = state
            changed = True
        if changed:
            self._draw()
        if kwargs:
            super().configure(**kwargs)

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return super().cget(key)


class RoundEntry(tk.Entry):
    """无边框输入框，外层由调用方画浅紫填充和下划线。"""

    def __init__(self, master, **kwargs):
        kwargs.setdefault("relief", "flat")
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bg", SURFACE)
        kwargs.setdefault("fg", TEXT)
        kwargs.setdefault("insertbackground", ACCENT)
        kwargs.setdefault("font", FONT_BODY)
        super().__init__(master, **kwargs)


class InputField(tk.Frame):
    """Material 文本框：浅紫圆角填充 + 底部下划线，标签浮在左侧。"""

    def __init__(self, master, label=None, textvariable=None, show=None, width=1, **kwargs):
        super().__init__(master, bg=CARD_BG, **kwargs)
        self._label_text = label or ""   # 仅作无障碍标识，不再画进输入框
        self._focus = False
        self._canvas = tk.Canvas(self, bg=CARD_BG, highlightthickness=0, bd=0,
                                 width=10, height=36)
        self._canvas.pack(fill=tk.X)
        self.entry = RoundEntry(self._canvas, textvariable=textvariable, show=show, width=width)
        self.entry.bind("<FocusIn>", lambda _: self._draw(True))
        self.entry.bind("<FocusOut>", lambda _: self._draw(False))
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event):
        self._draw(self._focus)

    def _draw(self, focus=False):
        self._focus = focus
        canvas = self._canvas
        w, h = self.winfo_width(), self.winfo_height()
        if w < 8 or h < 8:
            return
        canvas.delete("all")
        round_rect(canvas, 0, 0, w - 1, h - 3, min(12, h / 3), fill=SURFACE)
        # 底部下划线：聚焦时加粗成紫色
        canvas.create_rectangle(0, h - 3, w - 1, h - (1 if focus else 2),
                                fill=ACCENT if focus else "#CFC4E8", outline="")
        canvas.create_window(12, (h - 3) / 2, window=self.entry, anchor="w",
                             width=max(12, w - 24), height=max(10, h - 12))

    def focus_set(self):
        self.entry.focus_set()


class Toggle(tk.Canvas):
    """Material 开关：紫色轨道 + 圆形滑块。"""

    def __init__(self, master, variable=None, label="", command=None, **kwargs):
        bg = kwargs.pop("bg", None) or str(master.cget("bg")) or CARD_BG
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, **kwargs)
        self._var = variable
        self._label = label
        self._command = command
        self._width = 172
        self._height = 34
        self.configure(width=self._width, height=self._height)
        self.bind("<Button-1>", lambda _: self._click())
        if variable is not None:
            variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _draw(self):
        self.delete("all")
        on = bool(self._var.get()) if self._var is not None else False
        track = ACCENT if on else "#D3CFDB"
        knob = "#FFFFFF" if on else "#F7F7F9"
        round_rect(self, 0, 5, 46, 29, 12, fill=track)
        self.create_oval(23 if on else 7, 7, 39 if on else 23, 27, fill=knob, outline="")
        self.create_text(54, 17, anchor="w", text=self._label,
                         font=FONT_BODY, fill=TEXT if on else MUTED)

    def _click(self):
        if self._var is not None:
            self._var.set(not self._var.get())
        if self._command:
            self._command()


# ---------- 图形界面 ----------

class ICloudxApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("iCloudx 下载工具")
        self.geometry("920x560")
        self.minsize(860, 500)
        self.configure(bg=APP_BG)

        self.messages = queue.Queue()
        self.runner = None
        self.current_log = None
        self.current_success = False
        self.user_stopped = False
        self.password_visible = False
        self.default_args = DEFAULT_ARGS   # 默认参数只从配置文件读，不在界面显示

        self.var_user = tk.StringVar()
        self.var_domain = tk.StringVar(value="国区 cn")
        self.var_password = tk.StringVar()
        self.var_directory = tk.StringVar(value=str(APP_DIR))   # 默认当前目录
        self.var_binary = tk.StringVar(value=default_binary_path())
        self.var_custom_args = tk.StringVar()
        self.var_remember = tk.BooleanVar(value=False)
        self.var_2fa = tk.StringVar()
        self.var_status = tk.StringVar(value="就绪")

        self._build_ui()
        self._load_into_ui(load_config())
        self._refresh_toggles()
        self.after(120, self._poll_messages)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- 界面搭建 -----

    def _card(self, master, row, column, padx=0, pady=0, sticky="nsew"):
        card = Card(master)
        card.grid(row=row, column=column, sticky=sticky, padx=padx, pady=pady)
        return card.body

    def _build_ui(self):
        main = tk.Frame(self, bg=APP_BG)
        main.pack(fill=tk.BOTH, expand=True, padx=18, pady=12)
        main.columnconfigure(0, weight=1)

        # 单列紧凑布局：账号与选区 / 路径 / 自定义参数 / 操作
        account = self._card(main, 0, 0, pady=(0, 10), sticky="ew")
        self._build_account_card(account)

        paths = self._card(main, 1, 0, pady=(0, 10), sticky="ew")
        self._build_path_card(paths)

        params = self._card(main, 2, 0, pady=(0, 10), sticky="ew")
        self._build_params_card(params)

        actions = self._card(main, 3, 0, pady=(0, 10), sticky="ew")
        self._build_action_card(actions)

        # 双重认证：只在需要输入时出现
        prompt_card = Card(main, radius=16)
        prompt_card.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        self.prompt_frame = prompt_card.body
        self._build_prompt_card(self.prompt_frame)
        prompt_card.grid_remove()
        self.prompt_card = prompt_card

        # 实时日志：运行时才出现
        log_card = Card(main, radius=16)
        log_card.grid(row=5, column=0, sticky="nsew")
        main.rowconfigure(5, weight=1)
        self.log_frame = log_card.body
        self.log_text = scrolledtext.ScrolledText(
            self.log_frame, height=12, wrap=tk.WORD, bg="#FAF8FF", fg=TEXT,
            insertbackground=ACCENT, highlightthickness=0, bd=0, relief="flat",
            font=FONT_MONO,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.configure(state="disabled")
        log_card.grid_remove()
        self.log_card = log_card

        # 状态栏
        status = tk.Frame(main, bg=APP_BG)
        status.grid(row=6, column=0, sticky="ew", pady=(4, 0))
        tk.Label(status, textvariable=self.var_status, font=FONT_SMALL,
                 bg=APP_BG, fg=MUTED).pack(side=tk.LEFT)
        tk.Label(status, text=str(CONFIG_PATH), font=FONT_SMALL,
                 bg=APP_BG, fg="#B0B0B5").pack(side=tk.RIGHT)

        self.update_idletasks()
        self.geometry(f"920x{self.winfo_reqheight() + 24}")

    def _heading(self, parent, text):
        """Material 分区标签：紫色大写小字，像组件展示的左标签。"""
        tk.Label(parent, text=text, font=FONT_HEADING, bg=CARD_BG,
                 fg=ACCENT).pack(anchor="w", pady=(0, 9))

    def _field(self, parent, label, variable, row, column, width=24, show=None):
        """一行内嵌标签的输入条：标签与输入框同处一个胶囊。"""
        field = InputField(parent, label, variable, width=width, show=show)
        field.grid(row=row, column=column, sticky="ew", padx=(0, 8))
        return field

    def _build_account_card(self, body):
        self._heading(body, "ACCOUNT · 账号与选区")
        row = tk.Frame(body, bg=CARD_BG)
        row.pack(fill=tk.X)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(5, weight=1)
        tk.Label(row, text="用户名", font=FONT_SMALL, bg=CARD_BG, fg=MUTED).grid(row=0, column=0, padx=(0, 6))
        self._field(row, "邮箱 / +区号手机", self.var_user, 0, 1, width=22)
        tk.Label(row, text="选区", font=FONT_SMALL, bg=CARD_BG, fg=MUTED).grid(row=0, column=2, padx=(10, 6))
        seg = tk.Frame(row, bg=CARD_BG)
        seg.grid(row=0, column=3, sticky="w")
        self.seg_cn = PillButton(seg, text="国区 cn", command=lambda: self._set_domain("国区 cn"),
                                 height=30, width=82, font=FONT_SMALL, kind="outline", bg=CARD_BG)
        self.seg_cn.pack(side=tk.LEFT)
        self.seg_com = PillButton(seg, text="外区 com", command=lambda: self._set_domain("外区 com"),
                                  height=30, width=86, font=FONT_SMALL, kind="outline", bg=CARD_BG)
        self.seg_com.pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(row, text="密码", font=FONT_SMALL, bg=CARD_BG, fg=MUTED).grid(row=0, column=4, padx=(10, 6))
        pw_field = InputField(row, "iCloud 密码", self.var_password, show="•", width=14)
        pw_field.grid(row=0, column=5, sticky="ew", padx=(0, 8))
        self.entry_password = pw_field.entry
        self.btn_password = PillButton(row, text="显示", glyph="显", command=self._toggle_password,
                                       height=32, width=32, font=FONT_SMALL,
                                       kind="tonal", bg=CARD_BG)
        self.btn_password.grid(row=0, column=6, padx=(0, 6))
        self.btn_remember = Toggle(row, variable=self.var_remember, label="记住", bg=CARD_BG)
        self.btn_remember.grid(row=0, column=7)

    def _build_path_card(self, body):
        self._heading(body, "PATH · 路径")
        rows = tk.Frame(body, bg=CARD_BG)
        rows.pack(fill=tk.X)
        rows.columnconfigure(1, weight=1)
        tk.Label(rows, text="保存目录", font=FONT_SMALL, bg=CARD_BG, fg=MUTED).grid(row=0, column=0, padx=(0, 6), pady=4)
        InputField(rows, "照片将下载到", self.var_directory, width=22).grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=4)
        PillButton(rows, text="浏览…", command=self._choose_directory, kind="tonal",
                   height=32, width=74, font=FONT_SMALL, bg=CARD_BG).grid(row=0, column=2, padx=(0, 6), pady=4)
        tk.Label(rows, text="主程序", font=FONT_SMALL, bg=CARD_BG, fg=MUTED).grid(row=1, column=0, padx=(0, 6), pady=4)
        InputField(rows, "icloudpd 可执行文件", self.var_binary, width=18).grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=4)
        PillButton(rows, text="更改…", command=self._choose_binary, kind="tonal",
                   height=32, width=74, font=FONT_SMALL, bg=CARD_BG).grid(row=1, column=2, padx=(0, 6), pady=4)
        PillButton(rows, text="自动检测", command=self._reset_binary, kind="tonal",
                   height=32, width=82, font=FONT_SMALL, bg=CARD_BG).grid(row=1, column=3, pady=4)

    def _build_params_card(self, body):
        self._heading(body, "ARGUMENTS · 自定义参数（默认参数来自配置文件）")
        row = tk.Frame(body, bg=CARD_BG)
        row.pack(fill=tk.X)
        row.columnconfigure(0, weight=1)
        InputField(row, "例如 --recent 50", self.var_custom_args, width=18).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.btn_debug = PillButton(row, text="调试查看", command=self._show_debug, kind="tonal",
                                    height=32, width=84, font=FONT_SMALL, bg=CARD_BG)
        self.btn_debug.grid(row=0, column=1)

    def _build_action_card(self, body):
        self._heading(body, "ACTIONS · 操作")
        buttons = tk.Frame(body, bg=CARD_BG)
        buttons.pack(fill=tk.X)
        specs = (
            ("开始运行", self._on_run, 108, "primary"),
            ("停止", self._on_stop, 62, "outline"),
            ("打开保存目录", self._open_save_dir, 100, "outline"),
            ("保存配置", self._save_config, 76, "outline"),
            ("导入配置", self._import_config, 76, "outline"),
            ("导出配置", self._export_config, 76, "outline"),
            ("日志目录", self._open_log_dir, 76, "outline"),
            ("清空日志", self._clear_log, 76, "outline"),
        )
        for i, (label, command, width, kind) in enumerate(specs):
            button = PillButton(buttons, text=label, command=command, width=width,
                                height=34, kind=kind, font=FONT_BODY, bg=CARD_BG)
            button.grid(row=0, column=i, padx=(0, 7), sticky="w")
            if label == "开始运行":
                self.btn_run = button
                self.btn_run.configure(state="normal")
            elif label == "停止":
                self.btn_stop = button
                self.btn_stop.configure(state="disabled")

    def _build_prompt_card(self, body):
        self._heading(body, "2FA · 双重认证")
        row = tk.Frame(body, bg=CARD_BG)
        row.pack(fill=tk.X)
        field = InputField(row, "验证码 / 设备序号", self.var_2fa, width=22)
        field.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry_2fa = field.entry
        self.entry_2fa.bind("<Return>", lambda _: self._send_2fa())
        PillButton(row, text="发送", command=lambda: self._send_2fa(), height=32,
                   width=58, font=FONT_SMALL, kind="primary", bg=CARD_BG).pack(side=tk.LEFT, padx=(8, 0))
        PillButton(row, text="发短信 (a)", command=lambda: self._send_2fa("a"), height=32,
                   width=86, font=FONT_SMALL, kind="outline", bg=CARD_BG).pack(side=tk.LEFT, padx=(6, 0))

    # ----- 状态与选区 -----

    def _set_domain(self, value):
        self.var_domain.set(value)
        self._refresh_toggles()

    def _refresh_toggles(self):
        self.seg_cn.configure(kind="primary" if self.var_domain.get() == "国区 cn" else "outline")
        self.seg_com.configure(kind="primary" if self.var_domain.get() == "外区 com" else "outline")

    def _toggle_remember(self):
        self.var_remember.set(not self.var_remember.get())
        self._refresh_toggles()

    # ----- 配置 -----

    def _load_into_ui(self, cfg):
        self.var_user.set(cfg.get("username", ""))
        self.var_domain.set("国区 cn" if cfg.get("domain", "cn") == "cn" else "外区 com")
        self.var_password.set(cfg.get("password", ""))
        self.var_remember.set(bool(cfg.get("remember_password", False)))
        self.var_directory.set(cfg.get("directory") or str(APP_DIR))
        self.var_binary.set(cfg.get("binary_path") or default_binary_path())
        self.default_args = cfg.get("default_args", DEFAULT_ARGS)
        self.var_custom_args.set(cfg.get("custom_args", ""))

    def _collect_config(self):
        return {
            "domain": self.var_domain.get().strip().split()[-1],
            "username": self.var_user.get().strip(),
            "default_args": self.default_args,
            "custom_args": self.var_custom_args.get().strip(),
            "directory": self.var_directory.get().strip(),
            "binary_path": self.var_binary.get().strip() if self.var_binary.get().strip() != default_binary_path() else "",
            "remember_password": bool(self.var_remember.get()),
            "password": self.var_password.get() if self.var_remember.get() else "",
        }

    def _save_config(self, notify=True):
        try:
            cfg = self._collect_config()
            save_config(cfg)
            save_config(cfg, CONFIG_PATH.parent / f"iCloudx-{username_key(cfg['username'])}.json")
            if notify:
                self.var_status.set(f"配置已保存：{CONFIG_PATH}")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))

    def _import_config(self):
        path = filedialog.askopenfilename(title="导入配置文件", filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("配置文件内容不是对象")
        except (OSError, ValueError) as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        self._load_into_ui(load_config(Path(path)))
        self._refresh_toggles()
        self.var_status.set(f"已导入配置：{path}")

    def _export_config(self):
        initial = f"iCloudx-{username_key(self.var_user.get())}.json"
        path = filedialog.asksaveasfilename(title="导出配置文件", initialfile=initial,
                                            defaultextension=".json",
                                            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            save_config(self._collect_config(), Path(path))
            self.var_status.set(f"已导出配置：{path}")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))

    # ----- 辅助操作 -----

    def _toggle_password(self):
        self.password_visible = not self.password_visible
        self.entry_password.configure(show="" if self.password_visible else "•")
        self.btn_password.configure(text="隐藏" if self.password_visible else "显示",
                                    glyph="隐" if self.password_visible else "显")

    def _show_debug(self):
        window = tk.Toplevel(self)
        window.title("调试查看")
        window.geometry("780x330")
        window.configure(bg=APP_BG)
        text = scrolledtext.ScrolledText(
            window, wrap=tk.WORD, background="#F7F7F8", foreground=TEXT,
            insertbackground=TEXT, highlightthickness=0, borderwidth=0, relief="flat",
            font=FONT_MONO,
        )
        text.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        info = (
            f"默认参数（来自配置文件）：\n{self.default_args}\n\n"
            f"完整命令（密码已隐藏）：\n{format_command(self._preview_command())}\n\n"
            f"配置文件：{CONFIG_PATH}\n日志目录：{LOG_DIR}\n"
        )
        text.insert(tk.END, info)
        text.configure(state="disabled")
        PillButton(window, text="关闭", command=window.destroy, height=30, width=70,
                   font=FONT_SMALL, bg=APP_BG).pack(pady=(0, 12))

    def _open_path(self, path):
        target = str(path)
        if os.name == "nt":
            os.startfile(target)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])

    def _open_save_dir(self):
        path = self.var_directory.get().strip()
        if not path or not Path(path).is_dir():
            messagebox.showwarning("目录不可用", f"保存目录不存在或为空：\n{path or '（空）'}")
            return
        self._open_path(path)

    def _open_log_dir(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._open_path(LOG_DIR)

    # ----- 目录与命令 -----

    def _choose_directory(self):
        path = filedialog.askdirectory(title="选择 iCloud 照片保存目录")
        if path:
            self.var_directory.set(path)

    def _choose_binary(self):
        path = filedialog.askopenfilename(title="选择 icloudpd 主程序")
        if path:
            self.var_binary.set(path)

    def _reset_binary(self):
        self.var_binary.set(default_binary_path())

    def _preview_command(self):
        cfg = self._collect_config()
        return build_command(cfg["binary_path"] or default_binary_path(), cfg["domain"],
                             cfg["username"], cfg["password"] or "<密码>", cfg["directory"] or "<目录>",
                             cfg["default_args"], cfg["custom_args"])

    def _build_current_command(self):
        cfg = self._collect_config()
        return build_command(cfg["binary_path"] or default_binary_path(), cfg["domain"],
                             cfg["username"], cfg["password"], cfg["directory"],
                             cfg["default_args"], cfg["custom_args"])

    # ----- 运行与日志 -----

    def _append_log(self, text):
        self.log_text.configure(state="normal")
        at_bottom = self.log_text.yview()[1] > 0.95
        self.log_text.insert(tk.END, text + "\n")
        if at_bottom:
            self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def _on_run(self):
        cfg = self._collect_config()
        if not valid_username(cfg["username"]):
            messagebox.showwarning(
                "用户名格式不对",
                "用户名须为邮箱地址，或带区号的手机号。\n例如：name@example.com 或 +8613800138000",
            )
            return
        if not cfg["password"]:
            messagebox.showwarning("缺少信息", "请填写 iCloud 密码")
            return
        if not cfg["directory"]:
            messagebox.showwarning("缺少信息", "请选择保存目录")
            return
        binary = cfg["binary_path"] or default_binary_path()
        if not binary.strip():
            messagebox.showwarning("缺少信息", "请设置主程序路径")
            return
        if not Path(binary).is_file():
            messagebox.showerror(
                "找不到主程序",
                f"找不到可执行文件：\n{binary}\n"
                f"请把 {BIN_NAME} 放在「iCloudx 下载工具」同级目录，或点「更改…」手动选择。",
            )
            return

        cmd = self._build_current_command()
        self.current_log = LOG_DIR / f"icloudx-{datetime.now():%Y%m%d-%H%M%S}.log"
        self.current_success = False
        self.user_stopped = False
        self._clear_log()
        self.log_card.grid()                  # 运行时才显示日志区
        self.prompt_card.grid_remove()
        self.update_idletasks()
        self.geometry(f"920x{self.winfo_reqheight() + 24}")
        self._append_log(f"> {format_command(cmd)}")
        self._append_log(f"> 日志文件：{self.current_log}")
        self._save_config(notify=False)       # 每次运行顺便更新默认配置
        self.var_status.set(f"运行中… 日志：{self.current_log}")
        self._refresh_toggles()
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")

        self.runner = Runner(cmd, self.current_log, self.messages)
        self.runner.start()

    def _on_stop(self):
        if self.runner:
            self.user_stopped = True
            self.runner.stop()
            self._append_log("> 已发送停止信号，等待进程退出…")
            self.var_status.set("正在停止…")
            self._refresh_toggles()

    def _send_2fa(self, preset=None):
        value = preset if preset is not None else self.var_2fa.get().strip()
        if not value:
            return
        if not self.runner or not self.runner.send_input(value):
            messagebox.showwarning("无法发送", "进程已结束或不在等待输入")
            return
        self.var_2fa.set("")
        self.prompt_card.grid_remove()
        self.var_status.set("已发送验证码，等待 icloudpd 继续…")
        self._refresh_toggles()

    def _poll_messages(self):
        retry = False
        try:
            for _ in range(300):
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "success":
                    self.current_success = True
                elif kind == "prompt2fa":
                    self.prompt_card.grid()
                    self.var_2fa.set("")
                    self.var_status.set("等待双重认证输入…")
                    self._refresh_toggles()
                    self.after(50, self.entry_2fa.focus_set)
                elif kind == "error":
                    self._append_log(f"[启动失败] {payload}")
                elif kind == "done":
                    self.prompt_card.grid_remove()
                    self.btn_run.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.var_status.set(f"运行结束，返回码：{payload}；日志：{self.current_log}")
                    self._refresh_toggles()
                    self._append_log(f"> 进程退出，返回码：{payload}")
                    self.runner = None
                    if not self.user_stopped:
                        if self.current_success:
                            messagebox.showinfo("下载完成", "全部照片和视频已下载完毕。")
                        else:
                            retry = messagebox.askyesno(
                                "运行未完成",
                                f"进程退出码 {payload}，未检测到「{SUCCESS_MARKER}」。\n是否重试？",
                            )
        except queue.Empty:
            pass
        if retry:
            self.after(100, self._on_run)
        self.after(120, self._poll_messages)

    def _on_close(self):
        if self.runner and self.runner.is_alive():
            if not messagebox.askokcancel("仍在运行", "icloudpd 还在运行，确定要停止并退出吗？"):
                return
            self.runner.stop()
        self.destroy()


def main():
    ICloudxApp().mainloop()


if __name__ == "__main__":
    main()
