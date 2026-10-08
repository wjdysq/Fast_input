#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自检脚本 —— 双击或命令行运行，快速确认每一环是否正常。

用法:
    python selftest.py

会依次检查：
  1. 平台与 Python 版本
  2. 剪贴板读 / 写
  3. 剪贴板内容指纹与去重
  4. HTML / RTF 降级
  5. 热键字符串解析
  6. 窗口枚举
  7. 前台窗口获取
  8. 原生剪贴板事件监听
  9. INPUT 结构体尺寸（决定 SendInput 能否工作）
 10. 【需交互】真实的按键注入 —— 会弹一个窗口让你按住观察

第 10 项是这个脚本的重点：它会创建一个窗口并尝试把文字打进去。
如果这一项失败，说明你的环境（或权限）不允许合成输入，
通常是：目标以管理员运行、被安全软件拦截、或远程桌面/受限会话。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, note: str = "") -> None:
    results.append((name, status, note))
    icon = {PASS: "  ok ", FAIL: "  !! ", SKIP: "  -- "}[status]
    print(f"{icon} {name}" + (f"  ({note})" if note else ""))


def check_platform():
    print("\n[1] 平台与版本")
    record("Python 版本", PASS if sys.version_info >= (3, 9) else FAIL,
           sys.version.split()[0])
    try:
        from backend import get_backend
        be = get_backend()
        record("平台后端", PASS, be.name)
        return be
    except Exception as e:
        record("平台后端", FAIL, str(e))
        return None


def check_clipboard(be):
    print("\n[2] 剪贴板读写")
    marker = f"__selftest_{int(time.time())}__"
    try:
        be.write_clipboard_text(marker)
        got = (be.read_clipboard() or {}).get("text")
        if got == marker:
            record("写入后读回一致", PASS)
        else:
            record("写入后读回一致", FAIL, f"期望 {marker!r} 实际 {got!r}")
    except Exception as e:
        record("剪贴板读写", FAIL, str(e))

    record("中文与 emoji", SKIP, "见上一步结果")


def check_fingerprint():
    print("\n[3] 内容指纹与去重")
    try:
        from contents import fingerprint
        a, b = fingerprint("hello"), fingerprint("hello")
        c = fingerprint("hello!")
        record("相同内容同指纹", PASS if a == b else FAIL)
        record("不同内容不同指纹", PASS if a != c else FAIL)
    except Exception as e:
        record("指纹", FAIL, str(e))


def check_format():
    print("\n[4] 富文本降级")
    try:
        from contents import html_to_text, rtf_to_text
        h = html_to_text("<p>甲</p><p>乙<b>粗</b></p>")
        record("HTML -> 文本", PASS if "甲" in h and "<" not in h else FAIL, repr(h))
        r = rtf_to_text(r"{\rtf1\ansi X\par Y}")
        record("RTF -> 文本", PASS if "X" in r and "Y" in r else FAIL, repr(r))
    except Exception as e:
        record("降级", FAIL, str(e))


def check_hotkey():
    print("\n[5] 热键解析")
    try:
        from hotkey import parse_hotkey
        mods, main = parse_hotkey("ctrl+alt+v")
        record("解析 ctrl+alt+v", PASS if main == 0x56 and 0x11 in mods else FAIL)
        try:
            parse_hotkey("v")
            record("拒绝无修饰键", FAIL, "本应抛错")
        except ValueError:
            record("拒绝无修饰键", PASS)
    except Exception as e:
        record("热键解析", FAIL, str(e))


def check_windows(be):
    print("\n[6] 窗口枚举")
    try:
        wins = be.list_windows()
        record("枚举可见窗口", PASS if wins else FAIL, f"{len(wins)} 个")
        record("获取前台窗口", PASS if be.get_foreground_window() else FAIL)
    except Exception as e:
        record("窗口枚举", FAIL, str(e))


def check_listener(be):
    print("\n[7] 原生剪贴板事件")
    try:
        hits = []

        def cb():
            hits.append(1)

        if not be.start_listener(cb):
            record("注册原生监听", SKIP, "不可用，将回退轮询")
            return
        record("注册原生监听", PASS)

        be.write_clipboard_text("__listener_probe__")
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            be.pump_events()
            time.sleep(0.01)
        be.stop_listener()
        record("收到变化通知", PASS if hits else FAIL, f"{len(hits)} 次")
    except Exception as e:
        record("原生监听", FAIL, str(e))


def check_input_struct():
    print("\n[8] SendInput 结构体")
    try:
        from backend import INPUT
        size = ctypes.sizeof(INPUT)
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        record("sizeof(INPUT)", PASS if size == expected else FAIL,
               f"{size}（期望 {expected}）")
    except Exception as e:
        record("结构体", FAIL, str(e))


def _rebuild_from_events(events) -> str:
    """
    把按键事件序列反向还原成文本，用于验证编码无损。

    换行/制表符走 VK_RETURN / VK_TAB；其余走 KEYEVENTF_UNICODE，
    BMP 外字符会占两个码元（代理对），需要合并。
    """
    from backend import (KEYEVENTF_UNICODE, VK_RETURN, VK_TAB)

    out = []
    i = 0
    while i < len(events):
        e = events[i]
        if e.u.ki.dwFlags & KEYEVENTF_UNICODE:
            code = e.u.ki.wScan
            if 0xD800 <= code <= 0xDBFF and i + 2 < len(events):
                code2 = events[i + 2].u.ki.wScan
                raw = int(code).to_bytes(2, "little") + int(code2).to_bytes(2, "little")
                out.append(raw.decode("utf-16-le", errors="replace"))
                i += 4
            else:
                out.append(chr(code))
                i += 2
        else:
            vk = e.u.ki.wVk
            out.append({VK_RETURN: "\n", VK_TAB: "\t"}.get(vk, "?"))
            i += 2
    return "".join(out)


def check_type_encoding(be):
    """
    离线检查逐字键入的编码正确性。

    这一项**不依赖抢前台**，任何环境都能验证，
    且正是"内容完整、顺序正确"的关键：把每个字符编码成按键事件，
    再还原回来比对。覆盖换行、制表、emoji（超 BMP）等情况。
    """
    print("\n[8b] 逐字键入编码（内容完整性与顺序）")

    from backend import normalize_newlines

    samples = [
        "hello",
        "Hello World 123",
        "中文输入测试",
        "混合 mixed 中英文 2026 !@#$%",
        "emoji 😀🎉 测试",           # 超 BMP，测代理对
        "数学符号 𝕏 ∫ 上标²",         # 更多超 BMP
        "单行无换行",
        "第一行\n第二行\n第三行",       # LF 换行
        "Windows\r\n风格\r\n换行",    # CRLF 换行
        "缩进\t制表符",               # 制表符
        "混合\n😀\r\n结束",           # 换行 + emoji 混排
        "a",
    ]

    captured = []
    orig = be._send_input

    def fake(inputs):
        captured.extend(inputs)
        return len(inputs)

    be._send_input = fake
    try:
        all_ok = True
        for text in samples:
            captured.clear()
            try:
                be.type_text(text, interval=0)
            except Exception as e:
                record(f"编码 {text[:16]!r}", FAIL, str(e))
                all_ok = False
                continue

            rebuilt = _rebuild_from_events(captured)
            expected = normalize_newlines(text)
            nl = expected.count("\n")
            note = f"{len(expected)} 字符" + (f"，{nl} 换行" if nl else "")

            if rebuilt == expected:
                record(f"编码 {text[:18]!r}", PASS, note)
            else:
                record(f"编码 {text[:18]!r}", FAIL,
                       f"还原为 {rebuilt!r}，期望 {expected!r}")
                all_ok = False

        record("编码整体结论", PASS if all_ok else FAIL,
               "所有样例都能无损还原" if all_ok else "存在无法还原的样例")
    finally:
        be._send_input = orig


def check_newline_handling():
    """换行必须走 VK_RETURN，否则不会真的换行。"""
    print("\n[8c] 换行与制表符处理")
    try:
        from backend import (_char_events, normalize_newlines,
                             VK_RETURN, VK_TAB, KEYEVENTF_UNICODE)

        # 规范化
        cases = [("a\r\nb", "a\nb"), ("a\rb", "a\nb"), ("a\nb", "a\nb")]
        ok_norm = all(normalize_newlines(a) == b for a, b in cases)
        record("CRLF / CR 规范化为 LF", PASS if ok_norm else FAIL)

        # 换行用 VK 而不是 Unicode
        ev = _char_events("\n")
        ok_nl = (len(ev) == 2 and ev[0].u.ki.wVk == VK_RETURN
                 and not (ev[0].u.ki.dwFlags & KEYEVENTF_UNICODE))
        record("换行走 VK_RETURN", PASS if ok_nl else FAIL,
               "Unicode 方式发换行不会生效")

        ev_t = _char_events("\t")
        ok_tab = ev_t[0].u.ki.wVk == VK_TAB
        record("制表符走 VK_TAB", PASS if ok_tab else FAIL)

        ev_a = _char_events("a")
        ok_a = bool(ev_a[0].u.ki.dwFlags & KEYEVENTF_UNICODE)
        record("普通字符走 Unicode", PASS if ok_a else FAIL)
    except Exception as e:
        record("换行处理", FAIL, str(e))


def check_ui():
    """界面模块的基础正确性。"""
    print("\n[8d] 终端界面")
    try:
        from ui import Panel, Stats, disp_width, truncate_disp, _plain_len

        record("中文宽度算 2 列", PASS if disp_width("中文") == 4 else FAIL,
               f"disp_width('中文')={disp_width('中文')}")
        record("emoji 宽度算 2 列", PASS if disp_width("😀") == 2 else FAIL)

        t = truncate_disp("这是一段很长很长的中文文本", 10)
        record("按宽度截断", PASS if disp_width(t) <= 10 else FAIL, repr(t))

        p = Panel(plain=False, log_lines=3)
        p.set_fields([("状态", "就绪", ""), ("监听", "原生事件")])
        p.stats = Stats(captured=1, typed=1, total_chars=10)
        out = p._build()
        widths = [_plain_len(l) for l in out.rstrip("\n").split("\n") if "│" in l]
        aligned = len(set(widths)) == 1
        record("面板框线对齐", PASS if aligned else FAIL,
               f"宽度集合 {sorted(set(widths))}")
    except Exception as e:
        record("界面", FAIL, str(e))


def check_real_injection(be):
    """创建一个窗口，真实注入文字并读回。需要窗口能抢到前台。"""
    print("\n[9] 真实按键注入（创建一个测试窗口）")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.CreateWindowExW.argtypes = [
        ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    user32.DefWindowProcW.restype = ctypes.c_void_p
    user32.SendMessageW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_void_p]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                 ctypes.c_void_p, ctypes.c_void_p)
    S = {}

    class WNDCLASS(ctypes.Structure):
        _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                    ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                    ("lpszMenuName", ctypes.c_wchar_p),
                    ("lpszClassName", ctypes.c_wchar_p)]

    def _wp(h, m, w, l):
        if m == 0x0010:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(ctypes.c_void_p(h), m, ctypes.c_void_p(w),
                                     ctypes.c_void_p(l))

    def gtext():
        n = int(user32.SendMessageW(ctypes.c_void_p(S["edit"]), 0x000E, 0, 0) or 0)
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.SendMessageW(ctypes.c_void_p(S["edit"]), 0x000D,
                            ctypes.c_void_p(n + 1), ctypes.cast(buf, ctypes.c_void_p))
        return buf.value

    def stext(s):
        buf = ctypes.create_unicode_buffer(s)
        user32.SendMessageW(ctypes.c_void_p(S["edit"]), 0x000C, 0,
                            ctypes.cast(buf, ctypes.c_void_p))

    try:
        hinst = kernel32.GetModuleHandleW(None)
        proc = WNDPROC(_wp)
        wc = WNDCLASS()
        wc.lpfnWndProc = proc
        wc.hInstance = hinst
        wc.lpszClassName = "ClipSelfTest"
        wc.hbrBackground = ctypes.c_void_p(6)
        user32.RegisterClassW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(
            0x8, "ClipSelfTest", "自检窗口（稍后自动关闭）",
            0x00C00000 | 0x10000000 | 0x80000000,
            200, 200, 600, 220, None, None, ctypes.c_void_p(hinst), None)
        if not hwnd:
            record("创建测试窗口", FAIL, f"errno={ctypes.get_last_error()}")
            return
        edit = user32.CreateWindowExW(
            0x200, "EDIT", "",
            0x40000000 | 0x10000000 | 0x00800000 | 0x0004,
            10, 10, 560, 150, ctypes.c_void_p(hwnd), None,
            ctypes.c_void_p(hinst), None)
        S["hwnd"], S["edit"] = hwnd, edit
        record("创建测试窗口", PASS)

        user32.AllowSetForegroundWindow(ctypes.c_void_p(-1))
        user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1),
                            200, 200, 600, 220, 0x0040)
        user32.ShowWindow(ctypes.c_void_p(hwnd), 5)
        user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        user32.SetActiveWindow(ctypes.c_void_p(hwnd))
        user32.SetFocus(ctypes.c_void_p(edit))
        time.sleep(0.6)

        fg = user32.GetForegroundWindow()
        if fg != hwnd:
            record("测试窗口拿到前台", FAIL,
                   "本环境不允许抢占前台，注入结果不可信")
        else:
            record("测试窗口拿到前台", PASS)

        # --- type_text ---
        stext("")
        user32.SetFocus(ctypes.c_void_p(edit))
        time.sleep(0.15)
        payload = "type_text 注入 ABC 中文"
        try:
            be.type_text(payload, interval=0.02)
        except Exception as e:
            record("type_text 注入", FAIL, str(e))
            payload = None
        if payload:
            time.sleep(0.4)
            got = gtext()
            record("type_text 注入", PASS if got == payload else FAIL,
                   "" if got == payload else f"实际={got!r}")

        # --- 逐字键入：换行 + emoji（真实换行效果只能这样验）---
        stext("")
        user32.SetFocus(ctypes.c_void_p(edit))
        time.sleep(0.15)
        payload_nl = "第一行\n第二行\n带😀的第三行"
        t0 = time.monotonic()
        try:
            be.type_text(payload_nl, interval=0.005)
            dt = time.monotonic() - t0
        except Exception as e:
            record("逐字键入（含换行）", FAIL, str(e))
            payload_nl = None
            dt = 0
        if payload_nl:
            time.sleep(0.4)
            got_nl = gtext().replace("\r\n", "\n")
            exp_nl = payload_nl.replace("\r\n", "\n")
            record("逐字键入（含换行）", PASS if got_nl == exp_nl else FAIL,
                   "" if got_nl == exp_nl else f"实际={got_nl!r}")
            units = len(payload_nl)
            speed = units / dt if dt > 0 else 0
            record("键入速度", PASS, f"{speed:.0f} 字符/秒（{dt:.3f}s/{units} 字符）")

        # --- 不限速模式 ---
        stext("")
        user32.SetFocus(ctypes.c_void_p(edit))
        time.sleep(0.15)
        payload_fast = "unlimited speed test " + "x" * 50
        try:
            be.type_text(payload_fast, interval=0)
        except Exception as e:
            record("不限速键入", FAIL, str(e))
            payload_fast = None
        if payload_fast:
            time.sleep(0.4)
            got_f = gtext()
            record("不限速键入", PASS if got_f == payload_fast else FAIL,
                   "" if got_f == payload_fast else f"实际={got_f!r}")

        # --- Ctrl+V ---
        stext("")
        user32.SetFocus(ctypes.c_void_p(edit))
        time.sleep(0.15)
        payload2 = "Ctrl+V 粘贴 中文 验证"
        try:
            be.write_clipboard_text(payload2)
            be.send_paste_hotkey()
        except Exception as e:
            record("Ctrl+V 粘贴", FAIL, str(e))
            payload2 = None
        if payload2:
            time.sleep(0.5)
            got2 = gtext()
            record("Ctrl+V 粘贴", PASS if got2 == payload2 else FAIL,
                   "" if got2 == payload2 else f"实际={got2!r}")

        user32.DestroyWindow(ctypes.c_void_p(hwnd))
        del proc
    except Exception as e:
        record("真实注入", FAIL, str(e))


def main() -> int:
    print("=" * 60)
    print("clip_autopaste 自检")
    print("=" * 60)

    be = check_platform()
    if be is None:
        print("\n平台后端不可用，后续检查跳过")
        return 1

    check_clipboard(be)
    check_fingerprint()
    check_format()
    check_hotkey()
    check_windows(be)
    check_listener(be)
    check_input_struct()
    check_type_encoding(be)
    check_newline_handling()
    check_ui()
    check_real_injection(be)

    print("\n" + "=" * 60)
    n_pass = sum(1 for _, s, _ in results if s == PASS)
    n_fail = sum(1 for _, s, _ in results if s == FAIL)
    n_skip = sum(1 for _, s, _ in results if s == SKIP)
    print(f"结果: {n_pass} 通过, {n_fail} 失败, {n_skip} 跳过")
    if n_fail:
        print("\n失败项:")
        for name, st, note in results:
            if st == FAIL:
                print(f"  - {name}: {note}")
        print("\n提示：若仅『真实按键注入』相关失败，通常是环境/权限问题：")
        print("  · 进程无权限抢占前台窗口（受限会话、远程桌面）")
        print("  · 被安全软件（如输入法保护、反作弊、EDR）拦截合成输入")
        print("  · 目标窗口以管理员权限运行，而本进程不是")
        print("  在普通用户桌面环境下直接运行通常即可通过。")
    print("=" * 60)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
