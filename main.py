#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clip_autopaste —— 剪贴板监听 + 快速自动输入

默认行为：逐字符快速键入（形成连续输入效果），而不是瞬间粘贴。

用法:
    python main.py                     # 默认运行（逐字键入 200 字符/秒）
    python main.py --speed 300         # 更快的逐字键入
    python main.py --type-interval 0   # 不限速，尽可能快
    python main.py --method paste      # 改回 Ctrl+V 瞬间粘贴
    python main.py -c my.json          # 指定配置文件
    python main.py --hotkey ctrl+shift+p
    python main.py --mode auto --target title --title "记事本"
    python main.py --dry-run           # 只打印，不真的输入
    python main.py --list-windows      # 列出当前所有窗口，用于找标题
    python main.py --init-config       # 生成默认配置文件

按 Ctrl+C 退出。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from typing import Optional

# ---------- 依赖检查 ----------
if sys.version_info < (3, 9):
    print("需要 Python 3.9+，当前是 %s" % sys.version.split()[0], file=sys.stderr)
    sys.exit(2)

from backend import Backend, PlatformNotSupported, get_backend
from config import Config, DEFAULT_CONFIG_PATH, write_default_config
from contents import ContentProcessor, ClipContent
from hotkey import HotkeyListener, parse_hotkey
from target import Injector, TargetResolver
from ui import (Panel, PanelLogHandler, PlainLogHandler, Stats,
                enable_vt, setup_stdout_encoding, truncate_disp)


log = logging.getLogger("autopaste")


# ==========================================================================
# 主程序
# ==========================================================================

class ClipAutoPaste:
    def __init__(self, cfg: Config, backend: Backend,
                 use_ui: bool = True) -> None:
        self.cfg = cfg
        self.backend = backend
        self.use_ui = use_ui

        self.processor = ContentProcessor(cfg)
        self.resolver = TargetResolver(backend, cfg)
        self.injector = Injector(backend, cfg)

        self._running = False
        self._pending: Optional[ClipContent] = None      # 待粘贴的内容
        self._pending_deadline: float = 0.0              # delay 模式用
        self._lock = threading.Lock()
        self._hotkey: Optional[HotkeyListener] = None
        self._use_native = False

        # 界面与统计
        self.stats = Stats()
        self.panel: Optional[Panel] = None
        self._busy = False                               # 正在输入中
        self._progress: tuple[int, int] = (0, 0)         # (done, total)
        self._last_render = 0.0
        self._listener_desc = "未启动"
        self._clip_dirty = False        # 原生事件置位，主循环延迟处理
        self._clip_dirty_at = 0.0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def run(self) -> int:
        cfg = self.cfg

        problems = cfg.validate()
        if problems:
            for p in problems:
                log.error("配置问题: %s", p)
            return 2

        # 校验热键写法（早失败好过运行到一半才炸）
        if cfg.trigger_mode == "hotkey":
            try:
                parse_hotkey(cfg.hotkey)
            except ValueError as e:
                log.error("热键配置错误: %s", e)
                return 2

        # 初始化界面
        self._setup_ui()

        self._running = True
        self._install_signal_handlers()

        if self.panel:
            self.panel.add_log("INFO", "程序已启动，等待复制内容")
        else:
            log.info("=" * 58)
            log.info("剪贴板自动输入已启动")
            log.info("  平台      : %s", self.backend.name)
            log.info("  触发方式  : %s", self._describe_trigger())
            log.info("  目标      : %s", self._describe_target())
            log.info("  输入手法  : %s", self._describe_method())
            if cfg.dry_run:
                log.info("  ** dry-run 模式：不会真的注入按键 **")
            log.info("按 Ctrl+C 退出")
            log.info("=" * 58)

        # 启动热键
        if cfg.trigger_mode == "hotkey":
            self._hotkey = HotkeyListener(cfg.hotkey, self._on_hotkey)
            if not self._hotkey.start():
                log.error("热键监听启动失败，无法继续（可用 --mode delay 替代）")
                return 3

        # 启动剪贴板变化监听
        self._use_native = False
        if cfg.use_native_events:
            self._use_native = self.backend.start_listener(self._on_clipboard_change)
            if self._use_native:
                self._listener_desc = "原生事件"
            else:
                self._listener_desc = f"轮询 {cfg.poll_interval:.2f}s"
                log.info("原生事件不可用，回退到轮询")
        else:
            self._listener_desc = f"轮询 {cfg.poll_interval:.2f}s"

        self._refresh_panel()
        if self.panel:
            self.panel.render()

        try:
            self._main_loop()
        finally:
            self._shutdown()
        return 0

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _setup_ui(self) -> None:
        """配置日志去向：有界面就进面板，没有就用带色普通输出。"""
        cfg = self.cfg
        setup_stdout_encoding()
        vt_ok = enable_vt()

        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))

        if self.use_ui and vt_ok:
            self.panel = Panel(plain=False)
            handler = PanelLogHandler(self.panel)
        else:
            self.panel = None
            handler = PlainLogHandler(use_color=vt_ok)

        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)

        if self.use_ui and not vt_ok:
            print("提示：当前终端不支持 ANSI 颜色，已降级为纯文本输出。"
                  "建议用 Windows Terminal 或新版 PowerShell。")

    def _refresh_panel(self) -> None:
        """把最新状态刷进面板。"""
        p = self.panel
        if not p:
            return

        cfg = self.cfg
        col = p.color

        if self._busy:
            done, total = self._progress
            if total > 0:
                pct = done * 100 // total
                bar_len = 18
                filled = done * bar_len // total
                bar = "█" * filled + "░" * (bar_len - filled)
                status, status_col = f"输入中  {bar} {pct}%  {done}/{total}", col.FG_YELLOW
            else:
                status, status_col = "输入中…", col.FG_YELLOW
        else:
            status, status_col = "就绪", col.FG_GREEN

        target_desc = self._describe_target()
        if cfg.target_mode == "foreground":
            fg = self.backend.get_foreground_window()
            if fg:
                title = self.backend.get_window_title(fg)
                if title:
                    target_desc = f"前台 · {truncate_disp(title, 24)}"

        method = self._describe_method()
        if self.stats.last_elapsed > 0:
            method = (f"{method}   （上次 {self.stats.last_chars} 字符 / "
                      f"{self.stats.last_elapsed:.2f}s）")

        p.set_fields([
            ("状态", status, status_col),
            ("监听", self._listener_desc),
            ("触发", self._describe_trigger()),
            ("目标", target_desc),
            ("输入", method),
        ])
        p.stats = self.stats
        p.set_footer("Ctrl+C 复制  ·  切到目标窗口  ·  按热键输入  ·  Ctrl+C 退出程序")

    def _maybe_render(self, force: bool = False) -> None:
        """节流渲染，避免刷太勤吃 CPU。"""
        if not self.panel:
            return
        now = time.monotonic()
        if not force and now - self._last_render < 0.1:
            return
        self._last_render = now
        self._refresh_panel()
        self.panel.render()

    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame):
            self._running = False

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # 某些环境下注册不了，忽略

    def _shutdown(self) -> None:
        if self._hotkey:
            self._hotkey.stop()
        self.backend.stop_listener()
        if self.panel:
            self.panel.add_log("INFO", "已退出")
            self.panel.render()
            self.panel.close()
        else:
            log.info("已退出")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _main_loop(self) -> None:
        cfg = self.cfg
        last_native_seq = -1

        while self._running:
            if self._use_native:
                self.backend.pump_events()
                self._handle_dirty()
                time.sleep(0.02)
            else:
                time.sleep(cfg.poll_interval)
                self._poll_once()

            # delay 模式：到点就输入
            if cfg.trigger_mode == "delay":
                with self._lock:
                    if self._pending and time.monotonic() >= self._pending_deadline:
                        content = self._pending
                        self._pending = None
                    else:
                        content = None
                if content:
                    self._do_paste(content)

            self._maybe_render()

    def _poll_once(self) -> None:
        """轮询模式：读一次剪贴板，判断是否有新内容。"""
        try:
            raw = self.backend.read_clipboard()
        except Exception:
            log.exception("读剪贴板失败")
            return
        self._handle_raw(raw)

    def _handle_dirty(self) -> None:
        """
        原生事件触发的延迟读取。

        延迟约 30ms 再读，且用稳定读取（连续两次一致），
        两个措施一起挡住"内容还没写完就被读走"导致的丢字。
        """
        if not self._clip_dirty:
            return
        if time.monotonic() - self._clip_dirty_at < 0.03:
            return
        self._clip_dirty = False
        try:
            raw = self.backend.read_clipboard_stable()
        except Exception:
            log.exception("读剪贴板失败")
            return
        self._handle_raw(raw)

    # ------------------------------------------------------------------
    # 剪贴板变化处理
    # ------------------------------------------------------------------
    def _on_clipboard_change(self) -> None:
        """
        原生事件回调。

        注意：这里**不直接读剪贴板**。WM_CLIPBOARDUPDATE 触发时，
        写入方可能还没把数据写完（有些程序是先清空再逐个格式写入），
        立刻读会读到残缺内容 —— 这正是"丢字"的一个来源。
        改为置一个待读标记，由主循环稍后做稳定读取。
        """
        with self._lock:
            self._clip_dirty = True
            self._clip_dirty_at = time.monotonic()

    def _handle_raw(self, raw: dict) -> None:
        content = self.processor.process(raw)

        if not content:
            return
        if content.kind == "image":
            self._handle_image(content)
            return
        if not content.text:
            return
        if self.processor.is_duplicate(content):
            return
        if self.processor.is_own_content(content):
            log.debug("忽略自己写回剪贴板的内容")
            return

        self.stats.captured += 1
        self.stats.last_preview = content.text
        self.stats.last_chars = len(content.text)
        self.stats.last_time = time.strftime("%H:%M:%S")

        self.processor.mark_seen(content)

        # 换行数单独提示，因为换行是最容易出问题的地方
        nl = content.text.count("\n")
        extra = f"，{nl} 个换行" if nl else ""
        log.info("已捕获 %d 字符%s", len(content.text), extra)

        if self.cfg.trigger_mode == "auto":
            self._do_paste(content)
        elif self.cfg.trigger_mode == "delay":
            with self._lock:
                self._pending = content
                self._pending_deadline = time.monotonic() + self.cfg.delay_seconds
            log.info("将在 %.1fs 后自动输入（现在切到目标窗口）", self.cfg.delay_seconds)
        else:
            # hotkey 模式：存下来等热键，保证输入的就是"你刚复制的那份"
            with self._lock:
                self._pending = content

    def _handle_image(self, content: ClipContent) -> None:
        if self.cfg.image_action == "skip":
            log.debug("剪贴板是图片，按配置跳过")
        else:
            log.warning("图片保存功能未实现（image_action=save）")

    # ------------------------------------------------------------------
    # 触发动作
    # ------------------------------------------------------------------
    def _on_hotkey(self) -> None:
        """热键按下（在热键线程里执行）。"""
        with self._lock:
            content = self._pending

        if content is None:
            # 热键模式没有 pending，读当前剪贴板
            try:
                raw = self.backend.read_clipboard_stable()
            except Exception:
                log.exception("热键触发时读剪贴板失败")
                return
            content = self.processor.process(raw)
            if not content or not content.text:
                log.info("热键触发，但剪贴板里没有可用文本")
                return

        self._do_paste(content)

    # ------------------------------------------------------------------
    def _do_paste(self, content: ClipContent) -> None:
        """校验目标后，把输入动作放到后台线程执行，避免卡住界面刷新。"""
        if self._busy:
            log.warning("上一次输入还没结束，本次触发已忽略")
            return

        target = self.resolver.resolve()
        if target is None:
            log.warning("找不到输入目标，本次跳过")
            self.stats.failed += 1
            return
        if self.resolver.is_excluded(target):
            return

        if self.cfg.require_confirm:
            if not self._confirm(target, content):
                log.info("用户取消")
                return

        self._busy = True
        self._progress = (0, len(content.text))
        self.stats.last_target = target.title
        self._maybe_render(force=True)

        t = threading.Thread(target=self._paste_worker,
                             args=(target, content), daemon=True)
        t.start()

    def _paste_worker(self, target, content: ClipContent) -> None:
        """后台执行输入（可能耗时几秒）。"""
        try:
            result = self.injector.paste(
                target, content.text,
                on_progress=self._on_progress,
            )
            if result.ok:
                self.stats.typed += 1
                self.stats.total_chars += result.chars
                self.stats.last_chars = result.chars
                self.stats.last_elapsed = result.elapsed
                self.stats.last_time = time.strftime("%H:%M:%S")
                log.info("已输入 %d 字符 → %s（%.2fs，%.0f 字符/秒）",
                         result.chars, truncate_disp(target.title, 24),
                         result.elapsed, result.speed)
            else:
                self.stats.failed += 1
                if result.aborted:
                    log.warning("输入被中止：%s（已输入 %d 字符）",
                                result.reason, result.chars)
                else:
                    log.error("输入失败：%s", result.reason or "未知原因")
        except Exception as e:
            self.stats.failed += 1
            log.error("输入异常：%s", e)
        finally:
            self._busy = False
            self._progress = (0, 0)
            self._maybe_render(force=True)

    def _on_progress(self, done: int, total: int) -> None:
        """逐字键入的进度回调（在输入线程里调用）。"""
        self._progress = (done, total)
        self._maybe_render()

    def _confirm(self, target, content: ClipContent) -> bool:
        preview = content.text if len(content.text) <= 40 else content.text[:40] + "..."
        try:
            ans = input(f"\n输入到 [{target.title}]？内容 {preview!r} [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans in ("y", "yes")

    # ------------------------------------------------------------------
    # 描述
    # ------------------------------------------------------------------
    def _describe_trigger(self) -> str:
        m = self.cfg.trigger_mode
        if m == "hotkey":
            return f"手动热键 {self.cfg.hotkey}"
        if m == "auto":
            return "复制后立即自动粘贴"
        return f"复制后延迟 {self.cfg.delay_seconds}s 自动粘贴"

    def _describe_target(self) -> str:
        m = self.cfg.target_mode
        if m == "foreground":
            return "当前前台窗口"
        if m == "title":
            return f"标题匹配 {self.cfg.target_title!r}"
        return f"窗口句柄 0x{self.cfg.target_handle:X}"

    def _describe_method(self) -> str:
        c = self.cfg
        if c.paste_method == "paste":
            return "Ctrl+V 瞬间粘贴"
        iv = c.type_interval
        if iv <= 0:
            return "逐字符键入（不限速，尽可能快）"
        return f"逐字符键入（{iv * 1000:.0f}ms/字符，约 {1 / iv:.0f} 字符/秒）"


# ==========================================================================
# CLI
# ==========================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clip_autopaste",
        description="监听剪贴板，逐字快速输入到指定窗口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH,
                   help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）")
    p.add_argument("--init-config", action="store_true",
                   help="写入一份默认配置文件后退出")
    p.add_argument("--list-windows", action="store_true",
                   help="列出所有可见窗口的标题与句柄后退出")
    p.add_argument("--log-level", help="覆盖日志级别 DEBUG/INFO/WARNING/ERROR")
    p.add_argument("--plain", action="store_true",
                   help="不用彩色面板，退回普通文本输出")

    g = p.add_argument_group("覆盖配置文件中的设置")
    g.add_argument("--mode", choices=["hotkey", "auto", "delay"],
                   help="触发方式")
    g.add_argument("--hotkey", help="热键，如 ctrl+alt+v")
    g.add_argument("--delay", type=float, dest="delay_seconds",
                   help="delay 模式的延迟秒数")
    g.add_argument("--target", choices=["foreground", "title", "handle"],
                   help="目标指定方式")
    g.add_argument("--title", dest="target_title", help="目标窗口标题（部分匹配）")
    g.add_argument("--handle", type=int, dest="target_handle", help="目标窗口句柄")
    g.add_argument("--method", "--paste-method", dest="m_method",
                   choices=["paste", "type"],
                   help="输入手法：type=逐字符快速键入（默认）；paste=Ctrl+V 瞬间粘贴")
    g.add_argument("--speed", type=float, dest="m_speed",
                   help="逐字键入速度，单位「字符/秒」。越大越快。例：--speed 200")
    g.add_argument("--type-interval", type=float, dest="m_interval",
                   help="逐字键入间隔秒数（越小越快，0=不限速）。与 --speed 二选一")
    g.add_argument("--poll", type=float, dest="poll_interval",
                   help="轮询间隔秒数")
    g.add_argument("--no-native", action="store_true",
                   help="强制使用轮询，不用原生事件")
    g.add_argument("--dry-run", action="store_true", help="只打印，不真的输入")
    g.add_argument("--confirm", action="store_true",
                   help="每次输入前在终端确认")
    return p


def list_windows(backend: Backend) -> int:
    try:
        wins = backend.list_windows()
    except PlatformNotSupported as e:
        print(f"当前平台不支持窗口枚举: {e}", file=sys.stderr)
        return 1

    if not wins:
        print("没有找到可见窗口")
        return 0

    print(f"共 {len(wins)} 个可见窗口：\n")
    print(f"{'句柄':>12}  {'PID':>8}  标题")
    print("-" * 70)
    for w in wins:
        print(f"{w['handle']:>12}  {w['pid']:>8}  {w['title']}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # 后端先建，后面几个模式都要用
    try:
        backend = get_backend()
    except PlatformNotSupported as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1

    if args.init_config:
        path = write_default_config(args.config, overwrite=True)
        print(f"已写入默认配置: {path}")
        return 0

    if args.list_windows:
        return list_windows(backend)

    # 加载配置
    try:
        cfg = Config.load(args.config)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2

    # CLI 覆盖
    for key in ("mode", "hotkey", "delay_seconds", "target", "target_title",
                "target_handle", "poll_interval"):
        val = getattr(args, key, None)
        if val is None:
            continue
        field = {"mode": "trigger_mode", "target": "target_mode"}.get(key, key)
        setattr(cfg, field, val)

    if args.m_method:
        cfg.paste_method = args.m_method

    # --speed 和 --type-interval 二选一，--type-interval 优先
    if args.m_interval is not None:
        cfg.type_interval = max(0.0, args.m_interval)
    elif args.m_speed is not None:
        if args.m_speed <= 0:
            cfg.type_interval = 0.0
        else:
            cfg.type_interval = 1.0 / args.m_speed

    if args.no_native:
        cfg.use_native_events = False
    if args.dry_run:
        cfg.dry_run = True
    if args.confirm:
        cfg.require_confirm = True
    if args.log_level:
        cfg.log_level = args.log_level

    app = ClipAutoPaste(cfg, backend, use_ui=not args.plain)
    try:
        return app.run()
    except PlatformNotSupported as e:
        log.error("平台不支持: %s", e)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
