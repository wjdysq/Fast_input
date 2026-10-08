# -*- coding: utf-8 -*-
"""
终端界面。

零依赖，纯 ANSI 转义序列。做成一个实时刷新的面板：
顶部状态区 + 中部统计区 + 底部滚动日志。

设计取舍：
- 不每次全屏重绘（会闪），用光标归位 + 逐行覆盖 + 清行尾。
- 宽度自适应终端；Unicode 框线，但检测不到 VT 支持就自动降级成纯文本。
- 中文/全角字符按 2 列计算宽度，否则框线会错位。
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import sys
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------
# ANSI 工具
# --------------------------------------------------------------------------

class C:
    """颜色码。空字符串表示不启用颜色（降级模式）。"""
    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"

    FG_BLACK = "\x1b[30m"
    FG_RED = "\x1b[31m"
    FG_GREEN = "\x1b[32m"
    FG_YELLOW = "\x1b[33m"
    FG_BLUE = "\x1b[34m"
    FG_MAGENTA = "\x1b[35m"
    FG_CYAN = "\x1b[36m"
    FG_WHITE = "\x1b[37m"
    FG_GRAY = "\x1b[90m"

    BG_BLUE = "\x1b[44m"
    BG_GRAY = "\x1b[100m"


class NoColor:
    """降级用的空颜色对象，所有属性都是空串。"""
    def __getattr__(self, name):
        return ""


def enable_vt() -> bool:
    """
    在 Windows 控制台上启用 ANSI 转义序列处理。

    Windows 10 之前不支持；10+ 需要显式打开
    ENABLE_VIRTUAL_TERMINAL_PROCESSING，否则会看到一堆 ^[[1m 之类的乱码。
    """
    if sys.platform != "win32":
        return True  # Unix 终端默认支持

    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetStdHandle.restype = ctypes.c_void_p
        k.GetStdHandle.argtypes = [ctypes.c_ulong]
        k.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k.GetConsoleMode.restype = ctypes.c_bool
        k.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        k.SetConsoleMode.restype = ctypes.c_bool

        STD_OUTPUT_HANDLE = -11
        h = k.GetStdHandle(STD_OUTPUT_HANDLE & 0xFFFFFFFF)
        if not h or h == -1:
            return False

        mode = ctypes.c_ulong()
        if not k.GetConsoleMode(ctypes.c_void_p(h), ctypes.byref(mode)):
            return False

        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if not k.SetConsoleMode(
            ctypes.c_void_p(h), mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        ):
            return False
        return True
    except Exception:
        return False


def setup_stdout_encoding() -> None:
    """把 stdout 切成 UTF-8，否则中文和框线字符会乱码。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def disp_width(s: str) -> int:
    """字符串的显示宽度（中文/全角算 2 列）。"""
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def truncate_disp(s: str, max_w: int, ellipsis: str = "…") -> str:
    """按显示宽度截断，超出部分用省略号代替。"""
    if max_w <= 0:
        return ""
    if disp_width(s) <= max_w:
        return s

    ell_w = disp_width(ellipsis)
    budget = max_w - ell_w
    out = []
    used = 0
    for ch in s:
        cw = 0 if unicodedata.combining(ch) else (
            2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        )
        if used + cw > budget:
            break
        out.append(ch)
        used += cw
    return "".join(out) + ellipsis


def pad_disp(s: str, width: int) -> str:
    """按显示宽度右侧补空格。"""
    return s + " " * max(0, width - disp_width(s))


# --------------------------------------------------------------------------
# 统计数据
# --------------------------------------------------------------------------

@dataclass
class Stats:
    captured: int = 0          # 捕获次数
    typed: int = 0             # 成功输入次数
    total_chars: int = 0       # 累计输入字符数
    failed: int = 0            # 失败次数
    last_chars: int = 0
    last_elapsed: float = 0.0
    last_target: str = ""
    last_preview: str = ""
    last_time: str = ""

    @property
    def avg_speed(self) -> float:
        """平均速度（字符/秒），按最近一次输入估算。"""
        if self.last_elapsed <= 0:
            return 0.0
        return self.last_chars / self.last_elapsed


# --------------------------------------------------------------------------
# 面板
# --------------------------------------------------------------------------

class Panel:
    """
    实时状态面板。

    渲染结构（宽度 W）：
        ┌─ 标题 ────────────────────────┐
        │  状态 / 监听 / 触发 / 目标 / 输入  │
        ├───────────────────────────────┤
        │  统计数字                       │
        ├───────────────────────────────┤
        │  滚动日志（最近 N 条）            │
        └───────────────────────────────┘
        底部提示
    """

    def __init__(self, plain: bool = False, log_lines: int = 7,
                 title: str = "剪贴板自动输入") -> None:
        self.plain = plain
        self.log_lines = log_lines
        self.title = title

        self.color = NoColor() if plain else C
        self.stats = Stats()
        self.status_fields: list[tuple[str, str]] = []
        self.logs: deque[tuple[str, str]] = deque(maxlen=log_lines)
        self.footer = "Ctrl+C 退出"
        self._rendered = False
        self._last_height = 0

    # ------------------------------------------------------------------
    # 宽度
    # ------------------------------------------------------------------
    def _term_width(self) -> int:
        try:
            return max(60, min(shutil.get_terminal_size().columns, 110))
        except OSError:
            return 78

    # ------------------------------------------------------------------
    # 状态设置
    # ------------------------------------------------------------------
    def set_fields(self, fields: list) -> None:
        """
        设置状态区的键值对。

        每项是 (标签, 值) 或 (标签, 值, 颜色码)。
        值本身**不要**带 ANSI 转义 —— 否则宽度计算会失真、框线错位。
        需要上色就把颜色码放在第三项。
        """
        self.status_fields = fields

    def set_footer(self, text: str) -> None:
        self.footer = text

    def add_log(self, level: str, msg: str) -> None:
        import time as _t
        ts = _t.strftime("%H:%M:%S")
        self.logs.append((ts, msg))

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def render(self) -> None:
        if self.plain:
            return  # 降级模式下由 logging 自己输出，不做面板
        out = self._build()
        if not self._rendered:
            sys.stdout.write("\x1b[2J")   # 首帧清屏
            self._rendered = True
        sys.stdout.write("\x1b[H")        # 光标归位
        sys.stdout.write(out)
        sys.stdout.write("\x1b[J")        # 清掉下方残留
        sys.stdout.flush()

    def _build(self) -> str:
        W = self._term_width()
        inner = W - 2
        c = self.color
        L: list[str] = []

        def line(s: str = "") -> None:
            L.append(s + "\x1b[K\n")

        # ---- 顶部标题 ----
        t = f" {self.title} "
        head_fill = max(0, inner - disp_width(t) - 1)
        line(f"{c.FG_CYAN}┌─{c.BOLD}{t}{c.RESET}{c.FG_CYAN}"
             f"{'─' * head_fill}┐{c.RESET}")

        # ---- 状态区 ----
        for item in self.status_fields:
            k = item[0]
            v = item[1]
            col = item[2] if len(item) > 2 else ""
            label = pad_disp(k, 8)
            content = truncate_disp(str(v), inner - 12)
            painted = f"{col}{content}{c.RESET}" if col else content
            line(f"{c.FG_CYAN}│{c.RESET}  {c.FG_GRAY}{label}{c.RESET}"
                 f"{painted}{' ' * max(0, inner - 10 - disp_width(content))}"
                 f"{c.FG_CYAN}│{c.RESET}")

        # ---- 分隔 ----
        line(f"{c.FG_CYAN}├{'─' * inner}┤{c.RESET}")

        # ---- 统计区 ----
        s = self.stats
        stat_line = (
            f"{c.FG_GRAY}捕获{c.RESET} {c.FG_YELLOW}{s.captured}{c.RESET}"
            f"   {c.FG_GRAY}已输入{c.RESET} {c.FG_GREEN}{s.typed}{c.RESET}"
            f"   {c.FG_GRAY}字符{c.RESET} {c.FG_YELLOW}{s.total_chars}{c.RESET}"
            f"   {c.FG_GRAY}速度{c.RESET} {c.FG_CYAN}{s.avg_speed:.0f}{c.RESET}"
            f"{c.FG_GRAY} 字/秒{c.RESET}"
        )
        if s.failed:
            stat_line += f"   {c.FG_RED}失败 {s.failed}{c.RESET}"
        line(f"{c.FG_CYAN}│{c.RESET}  {stat_line}"
             f"{' ' * max(0, inner - 2 - _plain_len(stat_line))}"
             f"{c.FG_CYAN}│{c.RESET}")

        # ---- 分隔 ----
        line(f"{c.FG_CYAN}├{'─' * inner}┤{c.RESET}")

        # ---- 日志区 ----
        shown = list(self.logs)
        if not shown:
            shown = [("", f"{c.FG_GRAY}等待复制内容…{c.RESET}")]
        # 补空行，保持面板高度稳定
        while len(shown) < self.log_lines:
            shown.insert(0, ("", ""))

        for ts, msg in shown:
            if ts:
                prefix = f"{c.FG_GRAY}{ts}{c.RESET}  "
                # 时间戳宽度 + 两个空格
                prefix_w = disp_width(ts) + 2
                body = truncate_disp(msg, inner - 2 - prefix_w - 2)
                text = f"{prefix}{body}"
                used = 2 + prefix_w + disp_width(body)
            else:
                text = msg
                used = 2 + _plain_len(msg)
            line(f"{c.FG_CYAN}│{c.RESET}  {text}"
                 f"{' ' * max(0, inner - used)}{c.FG_CYAN}│{c.RESET}")

        # ---- 底部 ----
        line(f"{c.FG_CYAN}└{'─' * inner}┘{c.RESET}")
        line(f"  {c.FG_GRAY}{self.footer}{c.RESET}")

        return "".join(L)

    def close(self) -> None:
        if not self.plain:
            sys.stdout.write("\x1b[0m\n")
            sys.stdout.flush()

    # ------------------------------------------------------------------
    def print_startup(self, lines: list[str]) -> None:
        """非面板模式下的启动信息（plain 用）。"""
        for s in lines:
            print(s)


def _plain_len(s: str) -> int:
    """
    去掉 ANSI 转义后的显示宽度。

    注意要匹配**所有** CSI 序列（颜色、清行尾 \\x1b[K、光标移动等），
    不能只匹配颜色码 —— 否则 \\x1b[K 会被当成 3 个可见字符，
    导致框线对齐计算全错。
    """
    import re
    return disp_width(re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s))


# --------------------------------------------------------------------------
# logging 桥接
# --------------------------------------------------------------------------

class PanelLogHandler(logging.Handler):
    """把 logging 记录喂给面板的日志区，而不是直接打到终端。"""

    def __init__(self, panel: Panel) -> None:
        super().__init__()
        self.panel = panel

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        self.panel.add_log(record.levelname, msg)


class PlainLogHandler(logging.Handler):
    """降级模式：带简单配色的普通输出。"""

    _COLORS = {
        "DEBUG": "\x1b[90m",
        "INFO": "",
        "WARNING": "\x1b[33m",
        "ERROR": "\x1b[31m",
        "CRITICAL": "\x1b[1;31m",
    }

    def __init__(self, use_color: bool = True) -> None:
        super().__init__()
        self.use_color = use_color

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        import time as _t
        ts = _t.strftime("%H:%M:%S")
        col = self._COLORS.get(record.levelname, "") if self.use_color else ""
        rst = "\x1b[0m" if col else ""
        print(f"{col}{ts} [{record.levelname[0]}]{rst} {msg}")
