#!/usr/bin/env python3
"""iCloudx 指引程序 — a small launcher that drives the iCloudx binaries.

The launcher ships WITHOUT the icloudpd backend. It looks for the real
binary beside itself (``iCloudx-Win.exe`` on Windows, ``iCloudx-Mac`` on
Apple silicon, ``iCloudx-Mac-Intel`` on Intel), so the backend can be
replaced with a newer release without touching the launcher.

Everything the child writes on stdout lands in three places: the log panel,
``<username>.log`` next to the launcher, and the progress row - which is
parsed out of the logger lines icloudpd already prints at INFO level.
"""

from __future__ import annotations

import math
import os
import pathlib
import platform
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "iCloudx · iCloud 照片下载"

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
ARCH = platform.machine().lower()

# Tk has no alpha in canvas fills, so the glass look is built out of opaque
# greys. They only read as glass while they stay a visible ladder apart -
# the smoke test asserts the gaps so a palette tweak cannot silently
# flatten the surfaces again.
C_WINDOW = "#E4E5EC"
C_CARD = "#F1F2F7"
C_EDGE = "#9C9CB0"
C_SHADOW = "#C6C7D4"
C_SUNK = "#D5D7E1"
C_LOG = "#EDEFF4"
C_HILITE_BAND = ("#FFFFFF", "#FAFBFD", "#F5F6FA")
C_TEXT = "#1D1D1F"
C_TEXT_SEC = "#6E6E73"
C_TEXT_HINT = "#8E8E93"
C_BLUE = "#007AFF"
C_RED = "#D70015"
C_GREEN = "#248A3D"
C_ORANGE = "#C77400"

if IS_WINDOWS:
    F_UI = "Microsoft YaHei UI"
    F_MONO = "Consolas"
else:
    F_UI = "SF Pro Text"
    F_MONO = "Menlo"

# What the child blocks on. Verified against the 1.33.0 binary by unpacking
# its PYZ: the prompt strings are unchanged from 1.32.3.
PROMPTS: list[tuple[str, str, str]] = [
    (
        "Please enter two-factor authentication code that you received over SMS",
        "短信验证码",
        "请输入收到的 6 位短信验证码",
    ),
    (
        "Please enter two-factor authentication code or device index",
        "双重认证",
        "输入 6 位验证码，或输入设备编号让它改用短信发送",
    ),
    (
        "Please enter two-factor authentication code",
        "双重认证",
        "请输入受信任设备上显示的 6 位验证码",
    ),
    (
        "Please enter two-step authentication code",
        "两步验证",
        "请输入收到的 6 位验证码",
    ),
    (
        "Please choose an option",
        "选择受信任设备",
        "输入设备编号，列表见日志",
    ),
]

RE_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
RE_PHONE = re.compile(r"^\+[1-9]\d{6,14}$")
RE_WIN_PATH = re.compile(r"^[A-Za-z]:[\\/](?:[^\\/:*?\"<>|\r\n]+[\\/]?)*$")
RE_POSIX_PATH = re.compile(r"^/(?:[^/\0]+/?)*$")

# Progress lives in the INFO log lines the child already prints.
RE_LOG = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+\w+\s")
RE_STAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+\w+\s")
# "Downloading 123 original photos and videos to /dir ..." - the count may
# also be "the first" (with --recent) or "???" (unknown length).
RE_TOTAL = re.compile(r"\bDownloading (the first|\?\?\?|\d+) ")
RE_DOWNLOADED = re.compile(r"\sINFO\s+Downloaded ")
RE_ALL_DONE = re.compile(r"\bAll photos? (and videos? )?have been downloaded\b")
RE_AUTH_OK = re.compile(r"Authentication completed successfully")

MAX_LOG_CHARS = 200_000


def backend_names() -> list[str]:
    """Backend file names to try, best first."""
    if IS_WINDOWS:
        return ["iCloudx-Win.exe"]
    if ARCH == "arm64":
        # Apple silicon can also run the Intel build through Rosetta, so it
        # is a workable fallback when the arm binary is missing.
        return ["iCloudx-Mac", "iCloudx-Mac-Intel"]
    return ["iCloudx-Mac-Intel"]


def locate_backend() -> pathlib.Path | None:
    """Find the backend beside the launcher, walking up a few levels.

    A frozen .app sits three directories deep (iCloudx.app/Contents/MacOS),
    so the distribution folder is a couple of parents up. Walking up covers
    both that layout and a flat folder of files.
    """
    override = os.environ.get("ICLOUDX_BACKEND")
    if override:
        path = pathlib.Path(override)
        return path if path.is_file() else None
    if getattr(sys, "frozen", False):
        start = pathlib.Path(sys.executable).resolve().parent
    else:
        start = pathlib.Path(__file__).resolve().parent
    for directory in [start, *start.parents][:5]:
        for name in backend_names():
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def base_directory() -> pathlib.Path:
    """The folder the launcher lives in - where the log file is written."""
    override = os.environ.get("ICLOUDX_BASE")
    if override:
        return pathlib.Path(override)
    if getattr(sys, "frozen", False):
        start = pathlib.Path(sys.executable).resolve().parent
    else:
        start = pathlib.Path(__file__).resolve().parent
    return start


def safe_username(value: str) -> str:
    """A file-name-safe form of the account, for the log file name."""
    cleaned = re.sub(r"[\\/:*?\"<>|@+\s]", "_", value.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "run"


def normalize_directory(value: str) -> str:
    """Strip paste artefacts: wrapping quotes and stray whitespace."""
    return value.strip().strip("\"'")


def valid_username(value: str) -> bool:
    return bool(RE_EMAIL.match(value) or RE_PHONE.match(value))


def valid_directory(value: str) -> bool:
    if not value:
        return False
    expanded = pathlib.Path(value).expanduser()
    if IS_WINDOWS:
        return bool(RE_WIN_PATH.match(str(expanded)))
    return bool(RE_POSIX_PATH.match(str(expanded)))


def strip_to_log_start(text: str) -> str:
    """Cut the tail a prompt left glued to the front of the next line."""
    match = RE_STAMP.search(text)
    if match and match.start() > 0:
        return text[match.start() :]
    return text


def luminance(colour: str) -> int:
    """Perceived brightness of an ``#RRGGBB`` colour, 0-255."""
    r, g, b = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    return (r * 299 + g * 587 + b * 114) // 1000


def round_rect_path(
    x0: float, y0: float, x1: float, y1: float, r: float, steps: int = 8
) -> list[float]:
    corners = (
        (x0 + r, y0 + r, 180, 90),
        (x1 - r, y0 + r, 90, 0),
        (x1 - r, y1 - r, 0, -90),
        (x0 + r, y1 - r, -90, -180),
    )
    points: list[float] = []
    for cx, cy, start, end in corners:
        for step in range(steps):
            angle = math.radians(start + (end - start) * step / steps)
            points.append(cx + r * math.cos(angle))
            points.append(cy - r * math.sin(angle))
    return points


class PlaceholderEntry(ttk.Entry):
    """A native entry that shows grey hint text until it holds a value.

    Deliberately not bound to a StringVar: writing to the widget from a
    trace callback would feed back into the variable and erase the value
    (that bug cost the password field its contents once already).
    """

    def __init__(
        self,
        master: tk.Misc,
        placeholder: str = "",
        secret: bool = False,
        **kwargs: object,
    ) -> None:
        self.placeholder = placeholder
        self.secret = secret
        # "Preview" state lives here rather than in the widget's show option,
        # because _sync rewrites show on every keystroke and would otherwise
        # flip the field back to masked while the user is typing.
        self.preview = False
        kwargs.setdefault("font", (F_UI, 13))
        super().__init__(master, **kwargs)
        self._sync()
        self.bind("<FocusIn>", lambda _: self._sync())
        self.bind("<FocusOut>", lambda _: self._sync())

    def _sync(self) -> None:
        has_value = bool(self.get())
        if has_value:
            if self.get() == self.placeholder:
                self.delete(0, "end")
            self.configure(foreground=C_TEXT)
            if self.secret:
                self.configure(show="" if self.preview else "*")
            return
        if self.placeholder and self.get() != self.placeholder:
            self.delete(0, "end")
            self.insert(0, self.placeholder)
        self.configure(foreground=C_TEXT_HINT)
        if self.secret:
            self.configure(show="")

    def value(self) -> str:
        text = self.get()
        if text == self.placeholder:
            return ""
        return text.strip()

    def set(self, value: str) -> None:
        self.delete(0, "end")
        self.insert(0, value)
        self.configure(foreground=C_TEXT)

    def toggle_preview(self) -> None:
        """Show or mask the contents. Only meaningful with a real value."""
        if not self.value():
            return
        self.preview = not self.preview
        self.configure(show="" if self.preview else "*")


class GlassCard(tk.Canvas):
    """A rounded card with the macOS glass edge treatment.

    The body is a plain frame embedded in the canvas so the card can hold
    native widgets. On macOS embedded windows always draw above canvas
    items, so the painted fill never covers them.
    """

    def __init__(
        self, master: tk.Misc, pad: int = 14, radius: int = 12, **kwargs: object
    ) -> None:
        super().__init__(master, highlightthickness=0, bd=0, bg=C_WINDOW, **kwargs)
        self.pad = pad
        self.radius = radius
        self._width = 0
        self._height = 0
        self.body = tk.Frame(self, bg=C_CARD)
        self._window = self.create_window(pad, pad, window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self.bind("<Configure>", self._on_self)

    def _on_body(self, event: tk.Event) -> None:
        height = event.height + 2 * self.pad
        if height != self._height:
            self._height = height
            self.configure(height=height)
        width = self._width or event.width + 2 * self.pad
        self._paint(width, height)

    def _on_self(self, event: tk.Event) -> None:
        if event.width != self._width:
            self._width = event.width
            self.itemconfigure(
                self._window, width=max(1, event.width - 2 * self.pad)
            )
            self._paint(event.width, self._height)

    def _paint(self, width: int, height: int) -> None:
        self.delete("deco")
        if width < 2 or height < 2:
            return
        radius = min(self.radius, (width - 2) / 2, (height - 4) / 2)
        self.create_polygon(
            round_rect_path(0.5, 2.5, width - 0.5, height - 0.5, radius),
            fill=C_SHADOW,
            outline="",
            tags=("deco", "shade"),
        )
        self.create_polygon(
            round_rect_path(0.5, 0.5, width - 0.5, height - 2.5, radius),
            fill=C_CARD,
            outline=C_EDGE,
            width=1,
            tags=("deco", "fill"),
        )
        for row, colour in enumerate(C_HILITE_BAND):
            self.create_line(
                radius + 1,
                2.0 + row,
                width - radius - 1,
                2.0 + row,
                fill=colour,
                width=1,
                tags=("deco", "hilite"),
            )
        self.tag_lower("shade", self._window)


class Segmented(tk.Canvas):
    """A two-slot segmented control with a glass slide."""

    def __init__(
        self,
        master: tk.Misc,
        options: list[tuple[str, str]],
        variable: tk.StringVar,
        width: int = 220,
        height: int = 28,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            highlightthickness=0,
            bd=0,
            bg=C_CARD,
        )
        self.options = options
        self.variable = variable
        self.width = width
        self.height = height
        self._paint()
        self.bind("<Button-1>", self._click)
        variable.trace_add("write", lambda *_: self._paint())

    def _click(self, event: tk.Event) -> None:
        slot = int(event.x / (self.width / len(self.options)))
        slot = max(0, min(slot, len(self.options) - 1))
        self.variable.set(self.options[slot][0])

    def _paint(self) -> None:
        self.delete("all")
        current = self.variable.get()
        slot_w = self.width / len(self.options)
        radius = (self.height - 4) / 2
        x0, y0, x1, y1 = 0.5, 0.5, self.width - 0.5, self.height - 0.5
        self.create_polygon(
            round_rect_path(x0, y0, x1, y1, radius),
            fill=C_SUNK,
            outline=C_EDGE,
            width=1,
        )
        for index, (value, label) in enumerate(self.options):
            if value != current:
                self.create_text(
                    (index + 0.5) * slot_w,
                    self.height / 2,
                    text=label,
                    fill=C_TEXT_SEC,
                    font=(F_UI, 12),
                )
        for index, (value, label) in enumerate(self.options):
            if value != current:
                continue
            sx0 = index * slot_w + 2
            sx1 = (index + 1) * slot_w - 2
            sy0, sy1 = 2.5, self.height - 2.5
            sr = min(radius, (sx1 - sx0) / 2, (sy1 - sy0) / 2)
            self.create_polygon(
                round_rect_path(sx0, sy0, sx1, sy1, sr),
                fill=C_CARD,
                outline=C_EDGE,
                width=1,
            )
            for row, colour in enumerate(C_HILITE_BAND[:2]):
                self.create_line(
                    sx0 + sr,
                    sy0 + 1.5 + row,
                    sx1 - sr,
                    sy0 + 1.5 + row,
                    fill=colour,
                    width=1,
                )
            self.create_text(
                (index + 0.5) * slot_w,
                self.height / 2,
                text=label,
                fill=C_TEXT,
                font=(F_UI, 12),
            )


class App:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("760x640")
        self.root.minsize(680, 540)
        self.root.configure(bg=C_WINDOW)
        try:
            self.root.attributes("-alpha", 0.99)
        except tk.TclError:
            pass

        self.backend = locate_backend()
        self.queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.process: subprocess.Popen[bytes] | None = None
        self.pending_prompt = False
        self.started_at = 0.0
        self.running = False

        # Progress state, updated on the main thread while draining lines.
        self.total: int | None = None
        self.done = 0
        self.log_file = None

        self._style()
        self._build()

        if self.backend is None:
            self._log(
                "error",
                "没找到 iCloudx 主程序。请把本程序和 iCloudx-xxx 放在同一个文件夹里，"
                "或点「选主程序」手动指定。",
            )
        else:
            self._log("info", f"就绪 · 主程序 {self.backend.name}")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = None
        self._pump()

    def _style(self) -> None:
        style = ttk.Style()
        if IS_MAC:
            try:
                style.theme_use("aqua")
            except tk.TclError:
                pass
        self.root.option_add("*Font", (F_UI, 13))

    # ------------------------------------------------------------------ ui
    def _build(self) -> None:
        self.body = tk.Frame(self.root, bg=C_WINDOW)
        self.body.pack(fill="x", padx=18, pady=(14, 4))

        self._build_main_card()
        self._build_progress()
        self._build_log()

    def _label(self, master: tk.Misc, text: str) -> tk.Label:
        return tk.Label(master, text=text, bg=C_CARD, fg=C_TEXT, font=(F_UI, 12))

    def _hint(self, master: tk.Misc, text: str) -> tk.Label:
        return tk.Label(
            master, text=text, bg=C_CARD, fg=C_TEXT_HINT, font=(F_UI, 11)
        )

    def _error_label(self, master: tk.Misc) -> tk.Label:
        return tk.Label(master, text="", bg=C_CARD, fg=C_RED, font=(F_UI, 11))

    def _build_main_card(self) -> None:
        card = GlassCard(self.body)
        card.pack(fill="x")
        self.card_main = card
        inner = card.body

        inner.columnconfigure(1, weight=1)

        self._label(inner, "iCloud 账户").grid(row=0, column=0, sticky="w")
        self.entry_user = PlaceholderEntry(
            inner, PLACEHOLDER_USER, width=28
        )
        self.entry_user.grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=(4, 0))
        self.error_user = self._error_label(inner)
        self.error_user.grid(row=1, column=1, sticky="w", padx=(12, 0))
        self._hint(inner, "支持邮箱，或 +86 开头的手机号").grid(
            row=0, column=2, sticky="w", padx=(10, 0)
        )

        self._label(inner, "密码").grid(row=2, column=0, sticky="w", pady=(12, 0))
        passrow = tk.Frame(inner, bg=C_CARD)
        passrow.grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=(12, 0))
        passrow.columnconfigure(0, weight=1)
        self.entry_pass = PlaceholderEntry(passrow, secret=True)
        self.entry_pass.grid(row=0, column=0, sticky="ew")
        self.btn_preview = ttk.Button(
            passrow, text="预览", width=6, command=self._toggle_preview
        )
        self.btn_preview.grid(row=0, column=1, padx=(8, 0))
        self.error_pass = self._error_label(inner)
        self.error_pass.grid(row=3, column=1, sticky="w", padx=(12, 0))
        self._hint(inner, "区分大小写，只在本次运行使用").grid(
            row=2, column=2, sticky="w", padx=(10, 0)
        )

        self._label(inner, "账户区域").grid(row=4, column=0, sticky="w", pady=(12, 0))
        self.var_domain = tk.StringVar(value="cn")
        Segmented(
            inner,
            [("cn", "国内用户（云上贵州）"), ("com", "国外用户（iCloud.com）")],
            self.var_domain,
            width=380,
        ).grid(row=4, column=1, sticky="w", padx=(12, 0), pady=(12, 0))

        self._label(inner, "保存目录").grid(row=5, column=0, sticky="w", pady=(12, 0))
        dirrow = tk.Frame(inner, bg=C_CARD)
        dirrow.grid(row=5, column=1, sticky="ew", padx=(12, 0), pady=(12, 0))
        dirrow.columnconfigure(0, weight=1)
        self.entry_dir = PlaceholderEntry(dirrow, PLACEHOLDER_DIR)
        self.entry_dir.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            dirrow, text="选择…", width=8, command=self._pick_dir
        ).grid(row=0, column=1, padx=(8, 0))
        self.error_dir = self._error_label(inner)
        self.error_dir.grid(row=6, column=1, sticky="w", padx=(12, 0))
        self._hint(inner, "支持中文和空格路径，可放移动硬盘").grid(
            row=5, column=2, sticky="w", padx=(10, 0)
        )

        self.actions = tk.Frame(inner, bg=C_CARD)
        # grid, not pack: the card body already manages its rows with grid.
        self.actions.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(14, 2))
        self.btn_run = ttk.Button(
            self.actions, text="开始下载", command=self.start, default="active"
        )
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(
            self.actions, text="停止", command=self.stop, state="disabled"
        )
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_pick_backend = ttk.Button(
            self.actions, text="选主程序", command=self._pick_backend
        )
        self.btn_pick_backend.pack(side="left", padx=(8, 0))
        self.var_status = tk.StringVar(value="已就绪")
        tk.Label(
            self.actions,
            textvariable=self.var_status,
            bg=C_CARD,
            fg=C_TEXT_SEC,
            font=(F_UI, 12),
        ).pack(side="right")

    def _build_progress(self) -> None:
        card = GlassCard(self.body, pad=10, radius=10)
        card.pack(fill="x", pady=(10, 0))
        self.card_progress = card

        row = tk.Frame(card.body, bg=C_CARD)
        row.pack(fill="x")
        row.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(row, mode="determinate", maximum=100)
        self.bar.grid(row=0, column=0, sticky="ew")
        self.var_progress = tk.StringVar(value="等待开始")
        tk.Label(
            row,
            textvariable=self.var_progress,
            bg=C_CARD,
            fg=C_TEXT,
            font=(F_UI, 12),
        ).grid(row=0, column=1, padx=(12, 0))
        self.var_current = tk.StringVar(value="")
        tk.Label(
            card.body,
            textvariable=self.var_current,
            bg=C_CARD,
            fg=C_TEXT_HINT,
            font=(F_MONO, 10),
            anchor="w",
        ).pack(fill="x", pady=(6, 0))

    def _build_log(self) -> None:
        card = GlassCard(self.root, pad=1)
        card.pack(fill="both", expand=True, padx=18, pady=(12, 16))

        head = tk.Frame(card.body, bg=C_CARD)
        head.pack(fill="x", padx=12, pady=(10, 0))
        self.label_logfile = tk.Label(
            head, text="运行日志", bg=C_CARD, fg=C_TEXT, font=(F_UI, 12)
        )
        self.label_logfile.pack(side="left")
        ttk.Button(head, text="清空", command=self._clear_log).pack(side="right")

        wrap = tk.Frame(card.body, bg=C_LOG)
        wrap.pack(fill="both", expand=True, padx=1, pady=(8, 1))
        self.log = tk.Text(
            wrap,
            wrap="none",
            bg=C_LOG,
            fg=C_TEXT_SEC,
            font=(F_MONO, 11),
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=12,
            pady=8,
            spacing1=1,
            spacing3=1,
        )
        scroll = ttk.Scrollbar(wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        for name, colour in (
            ("info", C_TEXT_SEC),
            ("error", C_RED),
            ("warn", C_ORANGE),
            ("debug", C_TEXT_HINT),
            ("ok", C_GREEN),
            ("head", C_TEXT),
        ):
            self.log.tag_configure(name, foreground=colour)

    # ------------------------------------------------------------- helpers
    def _toggle_preview(self) -> None:
        self.entry_pass.toggle_preview()
        self.btn_preview.configure(text="隐藏" if self.entry_pass.preview else "预览")

    def _pick_dir(self) -> None:
        current = self.entry_dir.value() or str(pathlib.Path.home())
        chosen = filedialog.askdirectory(initialdir=current)
        if chosen:
            self.entry_dir.set(chosen)

    def _pick_backend(self) -> None:
        title = "选择 iCloudx 主程序"
        if IS_WINDOWS:
            chosen = filedialog.askopenfilename(
                title=title,
                filetypes=[("iCloudx 主程序", "iCloudx-Win.exe"), ("所有文件", "*.*")],
            )
        else:
            chosen = filedialog.askopenfilename(title=title)
        if chosen:
            self.backend = pathlib.Path(chosen)
            self._log("info", f"已指定主程序 {self.backend.name}")

    def _show_field_error(self, label: tk.Label, text: str) -> None:
        label.configure(text=text)

    def _validate(self) -> dict[str, str] | None:
        """Check every field; returns the values, or None with hints set."""
        ok = True
        username = self.entry_user.value()
        password = self.entry_pass.value()
        directory = normalize_directory(self.entry_dir.value())

        if not valid_username(username):
            self._show_field_error(
                self.error_user,
                "" if not username else "请输入有效邮箱，或 +86 开头的手机号",
            )
            ok = False
        else:
            self._show_field_error(self.error_user, "")

        if not password:
            self._show_field_error(self.error_pass, "密码不能为空")
            ok = False
        else:
            self._show_field_error(self.error_pass, "")

        if not valid_directory(directory):
            self._show_field_error(
                self.error_dir,
                "" if not directory else "请输入有效的绝对路径目录",
            )
            ok = False
        else:
            self._show_field_error(self.error_dir, "")

        if not ok:
            return None
        return {"username": username, "password": password, "directory": directory}

    def _build_argv(self, values: dict[str, str]) -> list[str]:
        # -u opens a per-user configuration group; the options after it
        # become defaults for that user. No --no-pause or other home-grown
        # flags here: the backend is a stock iCloudx build and would reject
        # them as unrecognized arguments.
        return [
            str(self.backend),
            "--live-photo-mov-filename-policy",
            "original",
            "--domain",
            self.var_domain.get(),
            "--password-provider",
            "parameter",
            "-u",
            values["username"],
            "-p",
            values["password"],
            "-d",
            values["directory"],
        ]

    def _mask(self, argv: list[str]) -> str:
        masked: list[str] = []
        skip = False
        for item in argv:
            if skip:
                masked.append("••••••")
                skip = False
                continue
            if item == "-p":
                skip = True
                masked.append("-p")
                continue
            masked.append(shlex.quote(item) if " " in item else item)
        return " ".join(masked)

    def _open_log_file(self, username: str) -> None:
        """Point the run log at <username>.log beside the launcher."""
        self._close_log_file()
        path = base_directory() / f"{safe_username(username)}.log"
        try:
            self.log_file = path.open("w", encoding="utf-8", errors="replace")
            self.label_logfile.configure(
                text=f"运行日志 · {path.name}",
                fg=C_TEXT,
            )
        except OSError as exc:
            self.log_file = None
            self._log("warn", f"无法创建日志文件 {path.name}：{exc}")

    def _close_log_file(self) -> None:
        if self.log_file is not None:
            try:
                self.log_file.close()
            except OSError:
                pass
            self.log_file = None

    # -------------------------------------------------------------- runner
    def start(self) -> None:
        if self.process is not None:
            return
        if self.backend is None:
            messagebox.showerror(APP_TITLE, "没找到 iCloudx 主程序，请先「选主程序」")
            return
        values = self._validate()
        if values is None:
            return

        target = pathlib.Path(values["directory"]).expanduser()
        if not target.is_dir():
            if messagebox.askyesno(APP_TITLE, "保存目录还不存在，现在创建它？"):
                try:
                    target.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    messagebox.showerror(APP_TITLE, f"创建目录失败：{exc}")
                    return
            else:
                return

        self.total = None
        self.done = 0
        self._reset_progress()
        self._open_log_file(values["username"])
        self._spawn(self._build_argv(values), "运行中…")

    def _spawn(self, argv: list[str], status: str) -> bool:
        self._log("head", "$ " + self._mask(argv))
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")

        # Downloaded files often lack the executable bit; the generated
        # .command scripts used to chmod before running, so do the same.
        if not IS_WINDOWS:
            binary = pathlib.Path(argv[0])
            try:
                if not os.access(binary, os.X_OK):
                    binary.chmod(0o755)
                    self._log("info", "已为主程序补上可执行权限 (chmod +x)")
            except OSError as exc:
                self._log("warn", f"无法设置可执行权限：{exc}")

        try:
            self.process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                env=env,
            )
        except OSError as exc:
            self._log("error", f"无法启动主程序：{exc}")
            self.process = None
            return False

        self.running = True
        self.pending_prompt = False
        self.started_at = time.time()
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="enabled")
        self.var_status.set(status)
        threading.Thread(target=self._read_loop, daemon=True).start()
        return True

    def _read_loop(self) -> None:
        proc = self.process
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        buffer = bytearray()
        while True:
            byte = stream.read(1)
            if not byte:
                break
            if byte == b"\n":
                self.queue.put(("line", buffer.decode("utf-8", "replace")))
                buffer.clear()
                continue
            buffer += byte
            text = buffer.decode("utf-8", "replace")
            if text.endswith("\r"):
                continue
            if self._match_prompt(text):
                buffer.clear()
                continue
            if len(buffer) > 8192:
                self.queue.put(("line", text))
                buffer.clear()

        if buffer:
            self.queue.put(("line", buffer.decode("utf-8", "replace")))
        self.queue.put(("exit", str(proc.wait())))

    def _match_prompt(self, text: str) -> bool:
        """Detect an interactive prompt. Runs on the reader thread."""
        for fragment, title, question in PROMPTS:
            if fragment in text:
                if not self.pending_prompt:
                    self.pending_prompt = True
                    self.queue.put(("line", text))
                    self.queue.put(("prompt", f"{title}\x1f{question}"))
                return True
        return False

    def _ask_code(self, title: str, question: str) -> None:
        self._log("head", f"› {title}")
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.configure(bg=C_WINDOW)
        dialog.geometry("430x200")
        dialog.resizable(False, False)
        dialog.grab_set()

        card = GlassCard(dialog, pad=1)
        card.pack(fill="both", expand=True, padx=16, pady=16)

        var = tk.StringVar()
        tk.Label(
            card.body, text=title, bg=C_CARD, fg=C_TEXT, font=(F_UI, 14)
        ).pack(pady=(20, 2))
        tk.Label(
            card.body, text=question, bg=C_CARD, fg=C_TEXT_SEC, font=(F_UI, 12)
        ).pack()

        field = ttk.Entry(
            card.body, textvariable=var, font=(F_UI, 16), justify="center", width=14
        )
        field.pack(pady=14)
        field.focus_set()

        def submit(_: tk.Event | None = None) -> None:
            value = var.get().strip()
            dialog.destroy()
            self.pending_prompt = False
            self._write_stdin(value + "\n")

        def cancel() -> None:
            dialog.destroy()
            self.pending_prompt = False
            self._write_stdin("\n")

        field.bind("<Return>", submit)
        dialog.protocol("WM_DELETE_WINDOW", cancel)

        bar = tk.Frame(dialog, bg=C_WINDOW)
        bar.pack(pady=(0, 16))
        ttk.Button(bar, text="取消", command=cancel).pack(side="left", padx=6)
        ttk.Button(bar, text="确认", command=lambda: submit(), default="active").pack(
            side="left", padx=6
        )

    def _write_stdin(self, payload: str) -> None:
        proc = self.process
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(payload.encode("utf-8"))
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass

    def stop(self) -> None:
        if self.process is None:
            return
        self._log("warn", "正在停止…")
        self.process.terminate()

    def _finish(self, code: int) -> None:
        self.process = None
        self.running = False
        self.btn_run.configure(state="enabled")
        self.btn_stop.configure(state="disabled")
        elapsed = time.time() - self.started_at
        if code == 0:
            self._log("ok", f"完成 · 用时 {elapsed:.0f} 秒")
            self.var_status.set("完成")
            if self.total is not None and self.done >= self.total:
                # Keep the last downloaded file visible - the summary alone
                # says nothing about what was actually fetched.
                self._set_progress(self.total, done=self.total)
        elif code < 0:
            self._log("warn", f"已中止 · 用时 {elapsed:.0f} 秒")
            self.var_status.set("已中止")
        else:
            self._log("error", f"退出码 {code} · 用时 {elapsed:.0f} 秒")
            self.var_status.set(f"退出码 {code}")
        self._close_log_file()

    # --------------------------------------------------------------- pump
    def _pump(self) -> None:
        drained: list[tuple[str, str]] = []
        while len(drained) < 400:
            try:
                drained.append(self.queue.get_nowait())
            except queue.Empty:
                break
        exit_code: int | None = None
        for kind, text in drained:
            if kind == "exit":
                exit_code = int(text)
            elif kind == "prompt":
                title, question = text.split("\x1f", 1)
                self._ask_code(title, question)
            else:
                self._append(text)
        if exit_code is not None:
            self._finish(exit_code)
        self._after_id = self.root.after(60, self._pump)

    def _append(self, line: str) -> None:
        text = strip_to_log_start(line.rstrip("\r"))
        self._track_progress(text)
        if not text.strip():
            return
        self._write_log_file(text)
        at_bottom = self.log.yview()[1] > 0.985
        self.log.insert("end", text + "\n", self._tag(text))
        if at_bottom:
            self.log.see("end")
        count = int(self.log.index("end-1c").split(".")[0])
        if count > 6000:
            self.log.delete("1.0", f"{count - 5000}.0")

    def _write_log_file(self, text: str) -> None:
        if self.log_file is None:
            return
        try:
            self.log_file.write(text + "\n")
            self.log_file.flush()
        except OSError:
            pass

    def _track_progress(self, text: str) -> None:
        """Parse progress out of a logger line. Main thread only."""
        match = RE_TOTAL.search(text)
        if match:
            raw = match.group(1)
            self.total = 0 if raw in ("???", "the first") else int(raw)
            self.done = 0
            self._set_progress(self.total, done=0)
            return
        if RE_DOWNLOADED.search(text):
            self.done += 1
            path = text.split("Downloaded ", 1)[-1].strip()
            self._set_progress(self.total, done=self.done, current=path)
            return
        if RE_ALL_DONE.search(text):
            if self.total is not None:
                self._set_progress(self.total, done=max(self.done, self.total))
            else:
                self.var_progress.set("全部下载完成")

    def _set_progress(
        self, total: int | None, done: int = 0, current: str | None = None
    ) -> None:
        if total:
            percent = min(100.0, done * 100.0 / total)
            self.bar.configure(mode="determinate", maximum=100, value=percent)
            self.var_progress.set(f"已下载 {done} / {total}")
        else:
            self.bar.configure(mode="indeterminate")
            self.bar.start(60)
            self.var_progress.set(f"已下载 {done} 张（总数未知）")
        if current is not None:
            shown = current if len(current) <= 70 else "…" + current[-69:]
            self.var_current.set(shown)

    def _reset_progress(self) -> None:
        try:
            self.bar.stop()
        except Exception:
            pass
        self.bar.configure(mode="determinate", maximum=100, value=0)
        self.var_progress.set("等待开始")
        self.var_current.set("")

    # ---------------------------------------------------------------- misc
    def _tag(self, text: str) -> str:
        upper = text.upper()
        if "CRITICAL" in upper or "ERROR" in upper or "FAILED" in upper:
            return "error"
        if "WARNING" in upper or "NOT INDEXED" in upper:
            return "warn"
        if "DEBUG" in upper:
            return "debug"
        if "ALL SET UP" in upper or "GREAT, YOU'RE ALL SET" in upper:
            return "ok"
        return "info"

    def _log(self, level: str, text: str) -> None:
        tag = {"head": "head", "ok": "ok", "error": "error", "warn": "warn"}.get(
            level, "info"
        )
        stamped = (
            text
            if level == "head"
            else f"[{time.strftime('%H:%M:%S')}] {text}"
        )
        self.log.insert("end", stamped + "\n", tag)
        self.log.see("end")
        self._write_log_file(stamped)

    def _clear_log(self) -> None:
        self.log.delete("1.0", "end")

    def _on_close(self) -> None:
        if self.process is not None:
            if not messagebox.askyesno(APP_TITLE, "任务还在运行，确定退出？"):
                return
            self.process.terminate()
        self.close()

    def close(self) -> None:
        if self._after_id:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self._close_log_file()
        self.root.destroy()


PLACEHOLDER_USER = "name@example.com 或 +8613800138000"
PLACEHOLDER_DIR = (
    r"D:\iCloudPhotos" if IS_WINDOWS else "/Users/you/Pictures/iCloudPhotos"
)


def main() -> None:
    App().root.mainloop()


if __name__ == "__main__":
    main()
