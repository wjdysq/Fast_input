# -*- coding: utf-8 -*-
"""
全局热键。

为什么要自己实现而不直接用 keyboard 库：
  - keyboard 库需要管理员权限才能在某些窗口上收到按键；
  - 它已经在 2023 年后基本停更；
  - 我们要的东西很简单，用 GetAsyncKeyState 轮询 30 行就够。

局限：GetAsyncKeyState 是轮询式，对"极短的按键"可能漏（人按一下通常 >80ms，
30ms 轮询足够）。要 100% 不丢需要 RegisterHotKey + 消息循环，代价是要独占
组合键。这里用轮询，牺牲极小概率的漏按，换来不独占、不冲突。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("autopaste")


# 修饰键 → 虚拟键码
_MODIFIER_VK = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "menu": 0x12,
    "shift": 0x10,
    "win": 0x5B, "super": 0x5B, "cmd": 0x5B, "meta": 0x5B,
}

# 常见命名键 → 虚拟键码
_NAMED_VK = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
}


def parse_hotkey(spec: str) -> tuple[set[int], int]:
    """
    "ctrl+alt+v" → ({0x11, 0x12}, 0x56)
    返回 (修饰键集合, 主键码)。解析失败抛 ValueError。
    """
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError(f"热键至少要有一个修饰键 + 一个主键，收到: {spec!r}")

    mods: set[int] = set()
    main: Optional[int] = None

    for p in parts:
        if p in _MODIFIER_VK:
            if main is not None:
                raise ValueError(f"修饰键不能出现在主键之后: {spec!r}")
            mods.add(_MODIFIER_VK[p])
        elif p in _NAMED_VK:
            if main is not None:
                raise ValueError(f"热键只能有一个主键: {spec!r}")
            main = _NAMED_VK[p]
        elif len(p) == 1 and p.isalnum():
            if main is not None:
                raise ValueError(f"热键只能有一个主键: {spec!r}")
            main = ord(p.upper())
        else:
            raise ValueError(f"无法识别的按键名: {p!r}（热键: {spec!r}）")

    if main is None:
        raise ValueError(f"热键缺少主键（如 v）: {spec!r}")
    if not mods:
        raise ValueError(f"热键至少需要一个修饰键（ctrl/alt/shift/win）: {spec!r}")

    return mods, main


class HotkeyListener:
    """
    后台线程轮询全局按键状态。
    只在 Windows 上工作；其他平台 start() 直接返回 False，上层回退。
    """

    def __init__(self, spec: str, callback: Callable[[], None],
                 poll_interval: float = 0.03) -> None:
        self.spec = spec
        self.callback = callback
        self.poll_interval = poll_interval

        self._mods, self._main = parse_hotkey(spec)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._armed = True   # 防止长按重复触发
        self._user32 = None

    # ------------------------------------------------------------------
    def available(self) -> bool:
        return sys.platform == "win32"

    def start(self) -> bool:
        if not self.available():
            log.warning("当前平台不支持内置全局热键监听（需要 Windows）")
            return False
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._thread = threading.Thread(
            target=self._loop, name="hotkey-listener", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        u = self._user32
        while not self._stop.is_set():
            all_down = all(u.GetAsyncKeyState(vk) & 0x8000 for vk in self._mods)
            main_down = bool(u.GetAsyncKeyState(self._main) & 0x8000)

            if all_down and main_down:
                if self._armed:
                    self._armed = False
                    try:
                        self.callback()
                    except Exception:
                        log.exception("热键回调异常")
            else:
                if not main_down:
                    self._armed = True

            self._stop.wait(self.poll_interval)
