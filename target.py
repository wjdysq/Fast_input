# -*- coding: utf-8 -*-
"""
目标定位 + 注入层。

这是整个程序风险最高的地方：模拟按键会落到"当前前台窗口"。
如果目标窗口没抢到焦点，粘贴就会打在别的窗口上（最坏情况是密码框）。
所以这里的策略是：
  1. 注入前先确定目标句柄
  2. 如果目标不是前台窗口，主动聚焦 + 等待，并二次确认
  3. 确认失败就放弃本次粘贴，而不是硬发按键
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from backend import Backend, TypeResult, normalize_newlines
from config import Config

log = logging.getLogger("autopaste")


def _units(text: str) -> int:
    """
    文本有多少个"输入单元"（= 规范化后的字符数）。

    一个输入单元占一个 interval 间隔。注意 BMP 外的字符（emoji）
    虽然要发 4 个按键事件（两个码元的 down/up），但它们在同一批里
    连续提交，只占**一个**间隔 —— 所以这里按字符数算，不是码元数。

    换行规范化也要计入：\\r\\n 算一个换行，不是两个。
    """
    return len(normalize_newlines(text))


# --------------------------------------------------------------------------
# 目标解析
# --------------------------------------------------------------------------

@dataclass
class Target:
    handle: int
    title: str
    resolved: bool = False


@dataclass
class PasteResult:
    """一次输入操作的结果。"""
    ok: bool = False
    chars: int = 0
    elapsed: float = 0.0
    aborted: bool = False     # 被中途中止（如目标失去焦点）
    reason: str = ""

    @property
    def speed(self) -> float:
        return self.chars / self.elapsed if self.elapsed > 0 else 0.0


class TargetResolver:
    def __init__(self, backend: Backend, cfg: Config) -> None:
        self.backend = backend
        self.cfg = cfg

    # ------------------------------------------------------------------
    def resolve(self) -> Optional[Target]:
        """按配置找出这次要粘到哪里。找不到返回 None。"""
        mode = self.cfg.target_mode

        if mode == "foreground":
            h = self.backend.get_foreground_window()
            if not h:
                log.warning("拿不到前台窗口句柄")
                return None
            return Target(handle=h, title=self.backend.get_window_title(h), resolved=True)

        if mode == "handle":
            h = int(self.cfg.target_handle)
            t = self.backend.get_window_title(h)
            if not t:
                log.warning("句柄 %s 无效或窗口已关闭", h)
                return None
            return Target(handle=h, title=t, resolved=True)

        if mode == "title":
            return self._resolve_by_title()

        return None

    def _resolve_by_title(self) -> Optional[Target]:
        needle = self.cfg.target_title.lower()
        wins = self.backend.list_windows()
        matches = [w for w in wins if needle in (w.get("title") or "").lower()]

        if not matches:
            log.warning("没有窗口标题匹配 %r", self.cfg.target_title)
            return None

        if len(matches) > 1:
            # 多个匹配 → 优先取当前就是前台的那个，否则取第一个并警告
            fg = self.backend.get_foreground_window()
            for w in matches:
                if w["handle"] == fg:
                    return Target(handle=w["handle"], title=w["title"], resolved=True)
            log.warning("标题 %r 匹配到 %d 个窗口，取第一个：%s",
                        self.cfg.target_title, len(matches), matches[0]["title"])

        w = matches[0]
        return Target(handle=w["handle"], title=w["title"], resolved=True)

    # ------------------------------------------------------------------
    def is_excluded(self, target: Target) -> bool:
        """目标标题命中排除列表 → 拒绝注入。用于防密码框之类的误伤。"""
        low = (target.title or "").lower()
        for pat in self.cfg.exclude_titles:
            if pat.lower() in low:
                log.warning("目标 %r 命中排除规则 %r，已跳过", target.title, pat)
                return True
        return False


# --------------------------------------------------------------------------
# 注入器
# --------------------------------------------------------------------------

class Injector:
    def __init__(self, backend: Backend, cfg: Config) -> None:
        self.backend = backend
        self.cfg = cfg

    def ensure_focus(self, target: Target, timeout: float = 0.6) -> bool:
        """
        确保目标在前台。返回 True 才可以发按键。
        Windows 的 SetForegroundWindow 有前台锁（foreground lock）限制：
        不是当前活跃进程调用时可能静默失败，所以必须二次校验。
        """
        if self.backend.get_foreground_window() == target.handle:
            return True

        log.debug("目标不在前台，尝试聚焦 0x%X", target.handle)
        self.backend.focus_window(target.handle)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.backend.get_foreground_window() == target.handle:
                time.sleep(0.05)  # 给窗口一点时间处理焦点切换
                return True
            time.sleep(0.02)

        log.warning("聚焦失败：目标 0x%X 没能成为前台窗口", target.handle)
        return False

    # ------------------------------------------------------------------
    def paste(self, target: Target, text: str,
              on_progress=None) -> "PasteResult":
        """
        把 text 送进 target。

        on_progress: 可选回调 on_progress(done, total)，逐字键入时报告进度。
        返回 PasteResult。
        """
        cfg = self.cfg
        total = _units(text)

        if cfg.dry_run:
            preview = text if len(text) <= 60 else text[:60] + "..."
            method = "逐字键入" if cfg.paste_method == "type" else "Ctrl+V 粘贴"
            log.info("[dry-run] 本会用「%s」把 %d 字符送到 %r",
                     method, len(text), target.title)
            log.info("[dry-run] 内容预览：%r", preview)
            if cfg.paste_method == "type" and cfg.type_interval > 0:
                log.info("[dry-run] 预计耗时 %.2fs（%.0f 字符/秒）",
                         total * cfg.type_interval, 1 / cfg.type_interval)
            return PasteResult(ok=True, chars=len(text), elapsed=0.0)

        if not self.ensure_focus(target):
            return PasteResult(ok=False, chars=0, elapsed=0.0,
                               reason="目标窗口无法获得焦点")

        try:
            if cfg.paste_method == "paste":
                t0 = time.monotonic()
                self.backend.send_paste_hotkey()
                return PasteResult(ok=True, chars=len(text),
                                   elapsed=time.monotonic() - t0)
            return self._type_out(target, text, on_progress)
        except Exception as e:
            log.error("输入失败：%s", e)
            return PasteResult(ok=False, chars=0, elapsed=0.0, reason=str(e))

    # ------------------------------------------------------------------
    def _make_focus_check(self, target: Target):
        """
        生成一个"目标还在前台吗"的检查函数。

        容忍连续两次瞬时抖动（比如目标窗口重绘导致的前台短暂切换），
        连续三次失焦才判定用户切走了窗口。
        """
        misses = [0]

        def check() -> bool:
            if self.backend.get_foreground_window() == target.handle:
                misses[0] = 0
                return True
            misses[0] += 1
            return misses[0] < 3

        return check

    def _type_out(self, target: Target, text: str,
                  on_progress=None) -> "PasteResult":
        """
        逐字符快速键入。

        包一层的原因：
        1. 输入前做内容校验，确保能原样输出（不丢字、不乱序）
        2. 输入中监控焦点，用户切走窗口就立刻中止，避免内容打进别处
        3. 向界面汇报进度
        """
        cfg = self.cfg
        n = len(text)
        interval = max(cfg.type_interval, 0)
        total = _units(text)

        # 输入前自检：确认这段文本能被无损地转成按键事件。
        ok, why = self._precheck(text)
        if not ok:
            log.error("内容校验未通过，已取消输入：%s", why)
            raise ValueError(f"内容无法完整输入：{why}")

        has_newline = "\n" in normalize_newlines(text)
        if interval > 0:
            log.info("开始键入 %d 字符到 %r（%.0f 字符/秒%s）",
                     n, target.title, 1 / interval,
                     "，含换行" if has_newline else "")
        else:
            log.info("开始键入 %d 字符到 %r（不限速%s）",
                     n, target.title, "，含换行" if has_newline else "")

        result: TypeResult = self.backend.type_text(
            text, interval,
            on_progress=on_progress,
            check_focus=self._make_focus_check(target),
        )

        if result.aborted:
            log.warning("输入被中止（%s）：已输入 %d/%d 字符，内容不完整",
                        result.reason, result.typed, result.total)
            return PasteResult(ok=False, chars=result.typed,
                               elapsed=result.elapsed, aborted=True,
                               reason=result.reason)

        log.info("输入完成：%d 字符，耗时 %.2fs（%.0f 字符/秒）",
                 n, result.elapsed, result.speed)
        return PasteResult(ok=True, chars=n, elapsed=result.elapsed)

    @staticmethod
    def _precheck(text: str) -> tuple[bool, str]:
        """
        输入前的内容校验。

        逐字键入的原理是把每个字符编码成按键盘事件，任何一步编码出错
        都会导致"打出来的东西和原文不一样"。这里先离线跑一遍
        编码 -> 解码 的往返，确认无损。

        覆盖的真实故障：
          · 超出 BMP 的字符（emoji）若没按代理对处理，码位会被截断
            —— 实测 😀 会变成 \uf600
          · 混入无法编码的代理项（lone surrogate）

        注意：换行和制表符走虚拟键码（VK_RETURN / VK_TAB），
        不参与 Unicode 编码，所以跳过它们单独校验。
        """
        if not text:
            return True, ""

        norm = normalize_newlines(text)
        for i, ch in enumerate(norm):
            if ch in ("\n", "\t"):
                continue
            try:
                units = ch.encode("utf-16-le", errors="strict")
            except UnicodeEncodeError:
                # 典型情况：孤立代理项（lone surrogate），本身就不是合法文本
                return False, (f"第 {i + 1} 个字符 {ch!r} 无法编码，"
                               "通常是孤立代理项，不是合法文本")

            if units.decode("utf-16-le", errors="strict") != ch:
                return False, (f"第 {i + 1} 个字符 {ch!r}(U+{ord(ch):04X}) "
                               "编码后无法还原")

        return True, ""
