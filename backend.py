# -*- coding: utf-8 -*-
"""
平台抽象层。

上层（monitor / injector）只调用这里定义的 Backend 接口，
各平台差异全部收敛在具体实现里。这是"多平台兼容"的唯一落点。

已实现：
  - WindowsBackend : 完整实现（ctypes 直调 Win32，零第三方依赖）
  - MacBackend     : 骨架 + 关键调用，需 pbcopy/pbpaste + pyobjc（可选）
  - LinuxBackend   : 骨架 + X11/Wayland 分支说明

未实现的平台会抛 PlatformNotSupported，而不是假装能跑。
"""

from __future__ import annotations

import abc
import ctypes
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional


class PlatformNotSupported(RuntimeError):
    pass


# ==========================================================================
# 输入结果
# ==========================================================================

@dataclass
class TypeResult:
    """一次逐字输入的结果。"""
    total: int = 0             # 应该输入的单元数
    typed: int = 0             # 实际输入的单元数
    elapsed: float = 0.0       # 耗时秒数
    aborted: bool = False      # 是否被中途中止（如目标失去焦点）
    reason: str = ""           # 中止原因

    @property
    def complete(self) -> bool:
        """是否完整输入。"""
        return not self.aborted and self.typed >= self.total

    @property
    def speed(self) -> float:
        """字符/秒。"""
        return self.typed / self.elapsed if self.elapsed > 0 else 0.0


# ==========================================================================
# SendInput 用的结构体（模块级定义，只算一次）
#
# 坑点：INPUT.u 是一个 union，64 位下最大成员是 MOUSEINPUT（32 字节），
# 加上 type（ULONG，4 字节）+ 4 字节对齐填充 = 40 字节。
# 如果用一个"占位 padding"近似 union，很容易算成 32 字节，
# SendInput 会因 dwSize 不匹配直接返回 0、静默什么都不发。
# 所以这里老老实实把三个成员都定义出来。
# ==========================================================================

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUT_UNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# 控制键虚拟键码。换行/制表符必须走 VK 而不是 KEYEVENTF_UNICODE ——
# Unicode 方式发 0x0D/0x0A 不会产生换行效果，多数控件只会忽略或留个不可见字符。
VK_RETURN = 0x0D
VK_TAB = 0x09

# 自检：不匹配的话后面所有注入都是静默失败，宁可启动就报错
_EXPECTED_INPUT_SIZE = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
assert ctypes.sizeof(INPUT) == _EXPECTED_INPUT_SIZE, (
    f"sizeof(INPUT)={ctypes.sizeof(INPUT)}，期望 {_EXPECTED_INPUT_SIZE}，"
    "SendInput 会拒绝执行"
)


def normalize_newlines(text: str) -> str:
    """
    把各种换行统一成单个 \\n。

    Windows 剪贴板里常见 \\r\\n；如果不规范化，逐字输入时
    会先发 \\r 再发 \\n，变成两个回车 —— 打出来就是两个空行。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _char_events(ch: str) -> list:
    """
    把单个字符编码成一组键盘事件（down, up, ...）。

    三种情况：
      · 换行 \\n  -> VK_RETURN 按下/抬起（必须用 VK，Unicode 方式无效）
      · 制表 \\t   -> VK_TAB
      · 其他      -> KEYEVENTF_UNICODE + wScan=码元

    关于代理对：wScan 是 USHORT，只能装 BMP 内码位。BMP 外的字符
    （emoji、部分汉字、数学符号）必须按 UTF-16 拆成两个码元分别发送，
    否则高位被截断（实测 😀 U+1F600 会变成 U+F600）。
    """
    def _mk(vk: int, scan: int, unicode_mode: bool, keyup: bool) -> "INPUT":
        e = INPUT()
        e.type = INPUT_KEYBOARD
        e.u.ki.wVk = vk
        e.u.ki.wScan = scan
        if unicode_mode:
            e.u.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if keyup else 0)
        else:
            e.u.ki.dwFlags = KEYEVENTF_KEYUP if keyup else 0
        e.u.ki.time = 0
        e.u.ki.dwExtraInfo = None
        return e

    # 换行：VK_RETURN
    if ch == "\n":
        return [_mk(VK_RETURN, 0, False, False), _mk(VK_RETURN, 0, False, True)]

    # 制表符：VK_TAB（Unicode 方式发 0x09 在多数控件里不会跳格）
    if ch == "\t":
        return [_mk(VK_TAB, 0, False, False), _mk(VK_TAB, 0, False, True)]

    # 其他：按 UTF-16 码元逐个发
    out: list = []
    units = ch.encode("utf-16-le", errors="strict")
    for i in range(0, len(units), 2):
        code = units[i] | (units[i + 1] << 8)
        out.append(_mk(0, code, True, False))
        out.append(_mk(0, code, True, True))
    return out


# ==========================================================================
# 接口
# ==========================================================================

class Backend(abc.ABC):
    """一个平台要提供的能力集合。"""

    name: str = "abstract"

    # ---------- 剪贴板读写 ----------
    @abc.abstractmethod
    def read_clipboard(self) -> dict:
        """
        返回 {"text": str|None, "html": str|None, "rtf": str|None,
              "image": bytes|None, "seq": int}
        seq 是变化序号，用于判断内容是否更新。做不到原生事件的平台用哈希代替。
        """

    @abc.abstractmethod
    def write_clipboard_text(self, text: str) -> None:
        ...

    def read_clipboard_stable(self, attempts: int = 3,
                              settle: float = 0.02) -> dict:
        """
        稳定读取剪贴板：连续读到相同内容才算数。

        为什么需要这个：WM_CLIPBOARDUPDATE 在 SetClipboardData 之后就发了，
        但有些程序是「先清空、再逐个格式写入」，此刻读到的可能是残缺内容；
        截图工具 / 剪贴板管理器也会并发改写剪贴板。
        连续两次读到一致的内容，基本可以确认已经写完。

        代价是多读一次 + 约 20ms 延迟 —— 相比"丢字"，这个代价值得。
        """
        import time as _t

        prev = self.read_clipboard()
        prev_key = (prev.get("text"), prev.get("html"), prev.get("rtf"))

        for _ in range(max(1, attempts - 1)):
            _t.sleep(settle)
            cur = self.read_clipboard()
            cur_key = (cur.get("text"), cur.get("html"), cur.get("rtf"))
            if cur_key == prev_key:
                return cur
            prev, prev_key = cur, cur_key

        return prev

    # ---------- 变化监听 ----------
    def start_listener(self, callback: Callable[[], None]) -> bool:
        """
        尝试注册原生剪贴板变化回调。返回 True 表示成功接管。
        返回 False 时上层回退到轮询。
        """
        return False

    def stop_listener(self) -> None:
        pass

    def pump_events(self) -> None:
        """原生事件模式下，每轮主循环调用一次，用于处理消息队列。"""
        pass

    # ---------- 窗口 ----------
    @abc.abstractmethod
    def list_windows(self) -> list[dict]:
        """返回 [{"handle": int, "title": str, "pid": int}, ...]"""

    @abc.abstractmethod
    def get_foreground_window(self) -> int:
        ...

    @abc.abstractmethod
    def focus_window(self, handle: int) -> bool:
        ...

    @abc.abstractmethod
    def get_window_title(self, handle: int) -> str:
        ...

    # ---------- 输入注入 ----------
    @abc.abstractmethod
    def send_paste_hotkey(self) -> None:
        """发送"粘贴"组合键（Windows/Linux: Ctrl+V, macOS: Cmd+V）"""

    @abc.abstractmethod
    def type_text(self, text: str, interval: float = 0.01,
                  on_progress=None, check_focus=None) -> "TypeResult":
        """逐字符输入。换行/制表必须转成对应按键，否则不会生效。"""


# ==========================================================================
# Windows
# ==========================================================================

class WindowsBackend(Backend):
    name = "windows"

    # --- Win32 常量 ---
    CF_UNICODETEXT = 13
    CF_TEXT = 1
    GMEM_MOVEABLE = 0x0002
    VK_CONTROL = 0x11
    VK_V = 0x56

    WM_CLIPBOARDUPDATE = 0x031D
    HWND_MESSAGE = -3

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise PlatformNotSupported("WindowsBackend 只能在 Windows 上使用")

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._hwnd_listener = None
        self._wndproc_ref = None      # 必须持有引用，否则回调被 GC 掉会崩
        self._callback: Optional[Callable[[], None]] = None
        self._message_class = None
        self._seq = 0                 # 我们自己维护的变化计数
        self._last_retries = 0        # 上次 _send_input 的重试次数（自适应用）

        self._setup_signatures()

    # ------------------------------------------------------------------
    def _setup_signatures(self) -> None:
        """ctypes 默认把返回值当 int，64 位下句柄会被截断，必须显式声明。"""
        u, k = self.user32, self.kernel32

        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.OpenClipboard.argtypes = [ctypes.c_void_p]
        u.OpenClipboard.restype = ctypes.c_bool
        u.CloseClipboard.restype = ctypes.c_bool
        u.GetClipboardData.argtypes = [ctypes.c_uint]
        u.GetClipboardData.restype = ctypes.c_void_p
        u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        u.SetClipboardData.restype = ctypes.c_void_p
        u.EmptyClipboard.restype = ctypes.c_bool
        u.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
        u.IsClipboardFormatAvailable.restype = ctypes.c_bool

        u.IsWindow.argtypes = [ctypes.c_void_p]
        u.IsWindow.restype = ctypes.c_bool
        u.IsWindowVisible.argtypes = [ctypes.c_void_p]
        u.IsWindowVisible.restype = ctypes.c_bool
        u.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        u.SetForegroundWindow.restype = ctypes.c_bool
        u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]

        k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k.GlobalAlloc.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = [ctypes.c_void_p]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [ctypes.c_void_p]
        k.GlobalFree.argtypes = [ctypes.c_void_p]
        k.GlobalSize.argtypes = [ctypes.c_void_p]
        k.GlobalSize.restype = ctypes.c_size_t

        # 这几个极其关键：句柄类返回值必须声明为 c_void_p，
        # 否则 64 位下默认按 c_int 处理会截断高位，CreateWindowExW 会报
        # "int too long to convert"。
        k.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        k.GetModuleHandleW.restype = ctypes.c_void_p

        # 窗口过程/窗口类的相关声明
        u.RegisterClassW.argtypes = [ctypes.c_void_p]
        u.RegisterClassW.restype = ctypes.c_ushort
        u.CreateWindowExW.argtypes = [
            ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        u.CreateWindowExW.restype = ctypes.c_void_p
        u.DefWindowProcW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p
        ]
        u.DefWindowProcW.restype = ctypes.c_void_p
        u.DestroyWindow.argtypes = [ctypes.c_void_p]
        u.AddClipboardFormatListener.argtypes = [ctypes.c_void_p]
        u.AddClipboardFormatListener.restype = ctypes.c_bool
        u.RemoveClipboardFormatListener.argtypes = [ctypes.c_void_p]
        u.RemoveClipboardFormatListener.restype = ctypes.c_bool
        u.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.EnumWindows.restype = ctypes.c_bool
        u.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
        u.SendInput.restype = ctypes.c_uint
        u.PeekMessageW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint
        ]
        u.PeekMessageW.restype = ctypes.c_bool
        u.TranslateMessage.argtypes = [ctypes.c_void_p]
        u.DispatchMessageW.argtypes = [ctypes.c_void_p]
        u.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
        u.RegisterClipboardFormatW.restype = ctypes.c_uint

    # ------------------------------------------------------------------
    # 剪贴板读
    # ------------------------------------------------------------------
    def _open_clipboard_with_retry(self, attempts: int = 5,
                                   delay: float = 0.02) -> bool:
        """
        OpenClipboard 会失败——只要有别的进程正开着剪贴板。
        这种情况非常常见（输入法、各种剪贴板工具、浏览器都在盯剪贴板），
        所以必须重试，不能一次失败就放弃。
        """
        for i in range(attempts):
            if self.user32.OpenClipboard(None):
                return True
            if i < attempts - 1:
                time.sleep(delay)
        return False

    def read_clipboard(self) -> dict:
        result = {"text": None, "html": None, "rtf": None, "image": None, "seq": self._seq}

        if not self._open_clipboard_with_retry():
            return result  # 重试也没抢到，跳过这一轮

        try:
            if self.user32.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
                result["text"] = self._get_unicode_text()

            html_fmt = self.user32.RegisterClipboardFormatW("HTML Format")
            if html_fmt and self.user32.IsClipboardFormatAvailable(html_fmt):
                result["html"] = self._get_bytes_as_text(html_fmt)

            rtf_fmt = self.user32.RegisterClipboardFormatW("Rich Text Format")
            if rtf_fmt and self.user32.IsClipboardFormatAvailable(rtf_fmt):
                result["rtf"] = self._get_bytes_as_text(rtf_fmt)

            if self.user32.IsClipboardFormatAvailable(15):  # CF_HDROP
                pass
            if self.user32.IsClipboardFormatAvailable(2):   # CF_BITMAP
                result["image"] = b""  # 只标记存在，不真的解 DIB
        finally:
            self.user32.CloseClipboard()

        return result

    def _get_unicode_text(self) -> Optional[str]:
        handle = self.user32.GetClipboardData(self.CF_UNICODETEXT)
        if not handle:
            return None
        ptr = self.kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.c_wchar_p(ptr).value or ""
        finally:
            self.kernel32.GlobalUnlock(handle)

    def _get_bytes_as_text(self, fmt: int) -> Optional[str]:
        handle = self.user32.GetClipboardData(fmt)
        if not handle:
            return None
        ptr = self.kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            size = self.kernel32.GlobalSize(handle)
            raw = ctypes.string_at(ptr, size)
            return raw.decode("utf-8", errors="replace").rstrip("\x00")
        finally:
            self.kernel32.GlobalUnlock(handle)

    # ------------------------------------------------------------------
    # 剪贴板写
    # ------------------------------------------------------------------
    def write_clipboard_text(self, text: str) -> None:
        """
        写入剪贴板。

        注意：剪贴板是共享资源，各种截图工具 / 剪贴板管理器 / 输入法都在监听它，
        我们写完之后可能立刻被别人覆盖（典型场景：PixPin 之类截图工具常驻监听）。
        所以这里写完会回读校验，并且重试整个"写 + 校验"过程。
        """
        data = text.encode("utf-16-le") + b"\x00\x00"
        size = len(data)

        last_err: Optional[str] = None
        for attempt in range(3):
            if not self._open_clipboard_with_retry(attempts=6, delay=0.03):
                last_err = "无法打开剪贴板（多次重试后被其他程序占用）"
                continue
            try:
                if not self.user32.EmptyClipboard():
                    last_err = "清空剪贴板失败"
                    continue

                h = self.kernel32.GlobalAlloc(self.GMEM_MOVEABLE, size)
                if not h:
                    last_err = "GlobalAlloc 失败"
                    continue
                ptr = self.kernel32.GlobalLock(h)
                if not ptr:
                    self.kernel32.GlobalFree(h)
                    last_err = "GlobalLock 失败"
                    continue
                ctypes.memmove(ptr, data, size)
                self.kernel32.GlobalUnlock(h)

                if not self.user32.SetClipboardData(self.CF_UNICODETEXT, h):
                    self.kernel32.GlobalFree(h)
                    last_err = "SetClipboardData 失败"
                    continue
                # 成功后所有权归系统，不能自己 free
            finally:
                self.user32.CloseClipboard()

            self._seq += 1

            # 回读校验：确认没被其他剪贴板监听程序立刻顶掉
            time.sleep(0.01)
            back = self.read_clipboard().get("text")
            if back == text:
                return
            last_err = f"写入后被其他程序覆盖（回读到 {back!r}）"

        raise RuntimeError(f"写入剪贴板失败：{last_err}")

    def bump_seq(self) -> None:
        """我们自己写入剪贴板后调用，让 seq 前进，配合 upper 层去重。"""
        self._seq += 1

    def current_seq(self) -> int:
        return self._seq

    # ------------------------------------------------------------------
    # 原生事件监听
    # ------------------------------------------------------------------
    def start_listener(self, callback: Callable[[], None]) -> bool:
        self._callback = callback
        try:
            self._create_message_window()
            if not self.user32.AddClipboardFormatListener(self._hwnd_listener):
                raise OSError(ctypes.get_last_error(), "AddClipboardFormatListener 失败")
            return True
        except OSError:
            self._hwnd_listener = None
            return False

    def stop_listener(self) -> None:
        if self._hwnd_listener:
            try:
                self.user32.RemoveClipboardFormatListener(self._hwnd_listener)
            except OSError:
                pass
            self.user32.DestroyWindow(self._hwnd_listener)
            self._hwnd_listener = None

    def _create_message_window(self) -> None:
        WNDPROCTYPE = ctypes.WINFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_void_p, ctypes.c_void_p,
        )

        def _proc(hwnd, msg, wparam, lparam):
            if msg == self.WM_CLIPBOARDUPDATE:
                self._seq += 1
                if self._callback:
                    try:
                        self._callback()
                    except Exception:
                        pass  # 回调里出错绝不能崩消息循环
            return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROCTYPE(_proc)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROCTYPE),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
            ]

        self._message_class = WNDCLASS()
        wc = self._message_class
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = self.kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "ClipAutoPasteListener"

        if not self.user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS，复用即可
                raise OSError(err, "RegisterClassW 失败")

        # HWND_MESSAGE(-3) 必须显式转成 c_void_p，直接传负 int 会溢出
        hwnd = self.user32.CreateWindowExW(
            0, wc.lpszClassName, "ClipAutoPaste", 0, 0, 0, 0, 0,
            ctypes.c_void_p(self.HWND_MESSAGE & 0xFFFFFFFFFFFFFFFF),
            None, wc.hInstance, None,
        )
        if not hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW 失败")
        self._hwnd_listener = hwnd

    def pump_events(self) -> None:
        """非阻塞地把消息队列抽干。"""
        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_void_p), ("lParam", ctypes.c_void_p),
                ("time", ctypes.c_uint), ("pt_x", ctypes.c_long), ("pt_y", ctypes.c_long),
            ]

        msg = MSG()
        PM_REMOVE = 0x0001
        while self.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))

    # ------------------------------------------------------------------
    # 窗口
    # ------------------------------------------------------------------
    def list_windows(self) -> list[dict]:
        out: list[dict] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )

        def _cb(hwnd, _):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if not title.strip():
                return True
            pid = ctypes.c_ulong()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.append({"handle": int(hwnd), "title": title, "pid": pid.value})
            return True

        self.user32.EnumWindows(WNDENUMPROC(_cb), None)
        return out

    def get_foreground_window(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def get_window_title(self, handle: int) -> str:
        length = self.user32.GetWindowTextLengthW(ctypes.c_void_p(handle))
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(ctypes.c_void_p(handle), buf, length + 1)
        return buf.value

    def focus_window(self, handle: int) -> bool:
        if not self.user32.IsWindow(ctypes.c_void_p(handle)):
            return False
        SW_RESTORE = 9
        self.user32.ShowWindow(ctypes.c_void_p(handle), SW_RESTORE)
        ok = self.user32.SetForegroundWindow(ctypes.c_void_p(handle))
        return bool(ok)

    # ------------------------------------------------------------------
    # 注入
    # ------------------------------------------------------------------
    def _send_input(self, inputs: list) -> int:
        """
        统一入口。返回成功注入的事件数。

        必须检查返回值——SendInput 会静默少注入（比如目标应用输入队列满了），
        少注入而不报错的话就会丢字符，这正是"最终内容不完整"的典型原因。

        做法：如果一次没提交完，就从未提交的部分继续提交；
        连续几次纹丝不动则判定为被拒绝，抛错而非静默丢字。

        副作用：把本次的重试次数记到 self._last_retries。
        上层据此判断"目标应用是不是处理不过来"，从而自适应降速。
        """
        u = self.user32
        remaining = list(inputs)
        total = len(remaining)
        done = 0
        stalled = 0
        retries = 0

        while remaining:
            n = len(remaining)
            arr = (INPUT * n)(*remaining)
            sent = u.SendInput(n, ctypes.byref(arr), ctypes.sizeof(INPUT))

            if sent == 0:
                stalled += 1
                retries += 1
                if stalled >= 3:
                    raise OSError(
                        ctypes.get_last_error(),
                        f"SendInput 被拒绝（已注入 {done}/{total} 个事件）。"
                        "常见原因：目标窗口以管理员权限运行而本进程不是（UIPI），"
                        "或被安全软件/输入法拦截了合成输入。"
                    )
                time.sleep(0.001)
                continue

            if sent < n:
                retries += 1     # 部分接受也算"目标吃力"
            stalled = 0
            done += sent
            remaining = remaining[sent:]

        self._last_retries = retries
        return done

    def _key_input(self, vk: int, keyup: bool = False) -> "INPUT":
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.u.ki.wVk = vk
        inp.u.ki.wScan = 0
        inp.u.ki.dwFlags = KEYEVENTF_KEYUP if keyup else 0
        inp.u.ki.time = 0
        inp.u.ki.dwExtraInfo = None
        return inp

    def _send_key(self, vk: int, keyup: bool = False) -> None:
        self._send_input([self._key_input(vk, keyup)])

    def send_paste_hotkey(self) -> None:
        # 一次性提交整个组合键，比逐个 sleep 更可靠（避免被目标窗口的重绘打断）
        self._send_input([
            self._key_input(self.VK_CONTROL, False),
            self._key_input(self.VK_V, False),
            self._key_input(self.VK_V, True),
            self._key_input(self.VK_CONTROL, True),
        ])

    def type_text(self, text: str, interval: float = 0.01,
                  on_progress=None, check_focus=None) -> "TypeResult":
        """
        逐字符快速输入，模拟"连续快速键入"而非瞬间粘贴。

        text        : 待输入文本。换行统一按 \\n 处理并以 VK_RETURN 发送，
                      制表符以 VK_TAB 发送（Unicode 方式发这两个不生效）。
        interval    : 字符间隔秒数。0 表示不限速。
        on_progress : 可选回调 on_progress(done, total)，用于界面进度条。
        check_focus : 可选回调，返回 False 表示目标已失去焦点 -> 立即中止。

        返回 TypeResult，说明实际输入了多少、是否被中止。

        四个关键设计：
        1. **换行走 VK**：换行/制表用虚拟键码，其余用 KEYEVENTF_UNICODE。
           这样既保证换行真的换行，又让中文/emoji 原样输入。
        2. **批量提交**：一次 SendInput 提交一批，而不是每字符一次。
        3. **精确计时**：Windows 的 sleep 粒度约 15.6ms，短间隔用忙等待微调。
        4. **自适应降速**：SendInput 出现重试（目标应用处理不过来）时自动
           放宽间隔，稳定后再逐步收回。这是解决"丢字"的主要手段。
        """
        if not text:
            return TypeResult(total=0, typed=0, elapsed=0.0)

        norm = normalize_newlines(text)
        # 每个"输入单元"= 一个字符（换行/制表各算一个）对应的一组事件
        unit_events = [_char_events(ch) for ch in norm]
        total_units = len(unit_events)

        start = time.perf_counter()

        # ---- 不限速：分大块连续提交，不等待 ----
        if interval <= 0:
            sent_units = 0
            CHUNK = 128
            for i in range(0, total_units, CHUNK):
                if check_focus is not None and not check_focus():
                    return TypeResult(total_units, sent_units,
                                      time.perf_counter() - start,
                                      aborted=True, reason="目标窗口失去焦点")
                part = unit_events[i:i + CHUNK]
                flat: list = []
                for evs in part:
                    flat.extend(evs)
                self._send_input(flat)
                sent_units += len(part)
                if on_progress:
                    on_progress(sent_units, total_units)
            return TypeResult(total_units, sent_units,
                              time.perf_counter() - start)

        # ---- 限速：分批 + 精确节拍 + 自适应降速 ----
        eff_interval = interval                      # 当前生效间隔（会自适应）
        min_interval = interval
        max_interval = max(interval * 4, 0.05)       # 最慢不超过 50ms

        sent_units = 0
        next_deadline = time.perf_counter()

        while sent_units < total_units:
            if check_focus is not None and not check_focus():
                return TypeResult(total_units, sent_units,
                                  time.perf_counter() - start,
                                  aborted=True, reason="目标窗口失去焦点")

            batch = self._calc_batch_size(eff_interval)
            part = unit_events[sent_units: sent_units + batch]
            flat = []
            for evs in part:
                flat.extend(evs)

            self._send_input(flat)
            n = len(part)
            sent_units += n

            if on_progress:
                on_progress(sent_units, total_units)

            # 自适应：SendInput 出现重试说明目标消化不了，放慢；
            # 稳定之后再逐步收回，兼顾速度与不丢字。
            if getattr(self, "_last_retries", 0) > 0:
                eff_interval = min(eff_interval * 1.6, max_interval)
            elif eff_interval > min_interval:
                eff_interval = max(min_interval, eff_interval * 0.92)

            if sent_units >= total_units:
                break

            next_deadline += eff_interval * n
            self._precise_wait_until(next_deadline)

        return TypeResult(total_units, sent_units, time.perf_counter() - start)

    @staticmethod
    def _calc_batch_size(interval: float) -> int:
        """
        每个间隔周期提交多少字符。

        间隔越大，可以攒越多一次性提交（反正是慢速输入）；
        间隔越小，批次要小一点，避免一次性灌太多导致目标应用处理不过来。
        上限 64 字符 —— 一次塞太多有触发目标应用输入队列溢出的风险。
        """
        if interval >= 0.05:
            return 64
        if interval >= 0.02:
            return 32
        if interval >= 0.01:
            return 16
        if interval >= 0.006:
            return 12
        return 8

    @staticmethod
    def _precise_wait_until(deadline: float) -> None:
        """
        等到 deadline（perf_counter 时间基准）。

        策略：先 sleep 掉大部分时间，剩下的用忙等待微调。
        纯 sleep 精度约 15ms，纯忙等待会吃满 CPU，两者结合最好。
        """
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        if remaining > 0.002:
            time.sleep(remaining - 0.001)
        # 最后 1ms 忙等待
        while time.perf_counter() < deadline:
            pass


# ==========================================================================
# macOS（骨架）
# ==========================================================================

class MacBackend(Backend):
    """
    未完整实现。要跑通需要：
      - pbcopy / pbpaste（系统自带，处理文本）
      - 剪贴板事件：NSPasteboard.changeCount 轮询（macOS 没有公开的变化通知）
      - 输入注入：Quartz CGEventPost，且进程必须在「系统设置 → 隐私与安全性 →
        辅助功能」里被授权，未授权时静默失败
      - 窗口操作：Quartz CGWindowListCopyWindowInfo
    """

    name = "macos"

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise PlatformNotSupported("MacBackend 只能在 macOS 上使用")

    def _has(self, cmd: str) -> bool:
        return shutil.which(cmd) is not None

    def read_clipboard(self) -> dict:
        text = None
        if self._has("pbpaste"):
            text = subprocess.run(
                ["pbpaste"], capture_output=True, timeout=2
            ).stdout.decode("utf-8", errors="replace")

        # macOS 没有公开的剪贴板变化通知，只能用 changeCount 轮询
        try:
            from AppKit import NSPasteboard  # type: ignore
            seq = NSPasteboard.generalPasteboard().changeCount()
        except ImportError:
            seq = hash(text or "")

        return {"text": text, "html": None, "rtf": None, "image": None, "seq": seq}

    def write_clipboard_text(self, text: str) -> None:
        if not self._has("pbcopy"):
            raise PlatformNotSupported("找不到 pbcopy")
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)

    def list_windows(self) -> list[dict]:
        try:
            from Quartz import (  # type: ignore
                CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
            )
        except ImportError as e:
            raise PlatformNotSupported("需要安装 pyobjc: pip install pyobjc-framework-Quartz") from e

        out = []
        for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID):
            out.append({
                "handle": int(w.get("kCGWindowNumber", 0)),
                "title": w.get("kCGWindowName", "") or "",
                "pid": int(w.get("kCGWindowOwnerPID", 0)),
            })
        return out

    def get_foreground_window(self) -> int:
        try:
            from AppKit import NSWorkspace  # type: ignore
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return int(app.processIdentifier())
        except ImportError:
            return 0

    def focus_window(self, handle: int) -> bool:
        raise PlatformNotSupported("macOS 激活指定窗口需要 pyobjc + 辅助功能授权")

    def get_window_title(self, handle: int) -> str:
        return ""

    def send_paste_hotkey(self) -> None:
        # macOS 的粘贴是 Cmd+V
        import subprocess as sp
        sp.run([
            "osascript", "-e",
            'tell application "System Events" to keystroke "v" using command down'
        ], check=False)

    def type_text(self, text: str, interval: float = 0.01,
                  on_progress=None, check_focus=None) -> "TypeResult":
        raise PlatformNotSupported("macOS 逐字输入需要 CGEventPost，未实现")


# ==========================================================================
# Linux（骨架）
# ==========================================================================

class LinuxBackend(Backend):
    """
    未完整实现。分两条路：
      - X11：xclip/xsel 读写剪贴板，xdotool key ctrl+v 注入。
             剪贴板变化监听可用 XFixes 或轮询 xclip -o。
      - Wayland：出于安全设计，普通程序读不到其他窗口的剪贴板，
             也无法向其他窗口发按键。只能依赖 wl-clipboard + ydotool，
             且需要相应权限（ydotool 走 uinput，通常要 root 或加入 input 组）。
    """

    name = "linux"

    def __init__(self) -> None:
        if not sys.platform.startswith("linux"):
            raise PlatformNotSupported("LinuxBackend 只能在 Linux 上使用")
        self.session = "wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11"

    def _tool(self, names: list[str]) -> Optional[str]:
        for n in names:
            if shutil.which(n):
                return n
        return None

    def read_clipboard(self) -> dict:
        tool = self._tool(["xclip", "xsel", "wl-paste"])
        if not tool:
            raise PlatformNotSupported("需要 xclip / xsel / wl-clipboard 之一")

        if tool == "xclip":
            cmd = ["xclip", "-selection", "clipboard", "-o"]
        elif tool == "xsel":
            cmd = ["xsel", "--clipboard", "--output"]
        else:
            cmd = ["wl-paste", "--no-newline"]

        try:
            text = subprocess.run(cmd, capture_output=True, timeout=2).stdout \
                .decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            text = ""

        return {"text": text, "html": None, "rtf": None, "image": None,
                "seq": hash(text)}

    def write_clipboard_text(self, text: str) -> None:
        tool = self._tool(["xclip", "xsel", "wl-copy"])
        if not tool:
            raise PlatformNotSupported("需要 xclip / xsel / wl-clipboard 之一")

        if tool == "xclip":
            cmd = ["xclip", "-selection", "clipboard"]
        elif tool == "xsel":
            cmd = ["xsel", "--clipboard", "--input"]
        else:
            cmd = ["wl-copy"]
        subprocess.run(cmd, input=text.encode("utf-8"), check=False)

    def list_windows(self) -> list[dict]:
        if self.session == "wayland":
            raise PlatformNotSupported("Wayland 不允许枚举其他窗口（安全设计）")
        raise PlatformNotSupported("X11 窗口枚举未实现，可接 python-xlib 或解析 wmctrl -l")

    def get_foreground_window(self) -> int:
        return 0

    def focus_window(self, handle: int) -> bool:
        raise PlatformNotSupported("未实现")

    def get_window_title(self, handle: int) -> str:
        return ""

    def send_paste_hotkey(self) -> None:
        if self.session == "wayland":
            tool = self._tool(["ydotool"])
            if not tool:
                raise PlatformNotSupported(
                    "Wayland 下需要 ydotool（走 uinput，通常要 root 或加入 input 组）"
                )
            subprocess.run(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"], check=False)
        else:
            tool = self._tool(["xdotool"])
            if not tool:
                raise PlatformNotSupported("X11 下需要 xdotool")
            subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], check=False)

    def type_text(self, text: str, interval: float = 0.01,
                  on_progress=None, check_focus=None) -> "TypeResult":
        tool = self._tool(["xdotool"]) if self.session == "x11" else self._tool(["ydotool"])
        if not tool:
            raise PlatformNotSupported("需要 xdotool 或 ydotool")
        norm = normalize_newlines(text)
        delay_ms = int(interval * 1000)
        t0 = time.perf_counter()
        # xdotool type 会处理换行；delay 是每字符毫秒数
        subprocess.run([tool, "type", "--delay", str(delay_ms), norm], check=False)
        return TypeResult(total=len(norm), typed=len(norm),
                          elapsed=time.perf_counter() - t0)


# ==========================================================================
# 工厂
# ==========================================================================

def get_backend() -> Backend:
    if sys.platform == "win32":
        return WindowsBackend()
    if sys.platform == "darwin":
        return MacBackend()
    if sys.platform.startswith("linux"):
        return LinuxBackend()
    raise PlatformNotSupported(f"不支持的平台: {sys.platform}")
