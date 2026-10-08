# -*- coding: utf-8 -*-
"""
内容处理层。

三件事：
1. 格式归一化：剪贴板里可能同时有 text / html / rtf / image 多种表示，
   按配置挑一个，并把富文本降级成纯文本（如果目标不支持）。
2. 去重：轮询和原生事件都可能重复报同一份内容，需要一个"指纹 + 时间窗"过滤。
3. 自触发防护：我们往剪贴板写东西时（restore_clipboard 场景），
   会触发自己的监听回调，必须认出来并忽略。
"""

from __future__ import annotations

import hashlib
import html as html_mod
import re
import time
from dataclasses import dataclass
from typing import Optional

from config import Config


@dataclass
class ClipContent:
    """归一化之后的剪贴板内容。"""
    kind: str                 # "text" | "image" | "empty"
    text: str = ""            # kind=text 时的最终文本
    raw: str = ""             # 原始文本（未做空白归一化）
    source_format: str = ""   # 内容实际来自哪种格式：text/html/rtf
    trunclated: bool = False  # 是否因为超长被截断
    fingerprint: str = ""     # 内容指纹

    def __bool__(self) -> bool:
        return self.kind != "empty"


# --------------------------------------------------------------------------
# HTML / RTF 降级
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAGS = ("</p>", "</div>", "</tr>", "</li>", "<br", "</h1>",
               "</h2>", "</h3>", "</h4>", "</h5>", "</h6>")


def html_to_text(raw_html: str) -> str:
    """
    把 HTML 降成可读纯文本。
    不做完整解析（不上 BeautifulSoup，避免依赖），够用就行：
      - 块级标签转成换行
      - 剥掉所有标签
      - 反转义实体
      - 压掉多余空行
    """
    if not raw_html:
        return ""

    # Windows 的 "HTML Format" 剪贴板格式带一段头部，正文从 <html 开始
    m = re.search(r"<html", raw_html, re.IGNORECASE)
    if m:
        raw_html = raw_html[m.start():]

    s = raw_html
    for tag in _BLOCK_TAGS:
        s = re.sub(re.escape(tag) + r"[^>]*>", "\n", s, flags=re.IGNORECASE)
    s = _TAG_RE.sub("", s)
    s = html_mod.unescape(s)

    s = s.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def rtf_to_text(raw_rtf: str) -> str:
    """
    极简 RTF 降级。只处理 \\par 换行和转义字符，够应付常见复制来源。
    要更完整就用 striprtf 库。
    """
    if not raw_rtf:
        return ""

    s = raw_rtf
    s = re.sub(r"\\par\b", "\n", s)
    s = re.sub(r"\\'([0-9a-fA-F]{2})",
               lambda m: bytes([int(m.group(1), 16)]).decode("latin-1"), s)
    s = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", s)   # 丢掉控制字
    s = s.replace("{", "").replace("}", "")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def normalize_ws(text: str) -> str:
    """压掉行尾空格与连续空行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# 指纹
# --------------------------------------------------------------------------

def fingerprint(text: str) -> str:
    """
    内容指纹。用 sha1 而不是 hash()：
    hash() 对 str 有进程级随机盐，跨进程不稳定，不适合写进日志比对。
    """
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 处理器
# --------------------------------------------------------------------------

class ContentProcessor:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._last_fp: Optional[str] = None
        self._last_time: float = 0.0

    # ------------------------------------------------------------------
    def process(self, clip: dict) -> ClipContent:
        """把 backend 读到的原始字典，转成 ClipContent。"""
        cfg = self.cfg

        text: Optional[str] = None
        source = ""

        if cfg.prefer_format == "html" and clip.get("html"):
            text = html_to_text(clip["html"])
            source = "html"
        elif cfg.prefer_format == "rtf" and clip.get("rtf"):
            text = rtf_to_text(clip["rtf"])
            source = "rtf"
        elif clip.get("text"):
            text = clip["text"]
            source = "text"
        elif clip.get("html"):
            # 配置要 text 但只有 html，降级处理
            text = html_to_text(clip["html"])
            source = "html"
        elif clip.get("rtf"):
            text = rtf_to_text(clip["rtf"])
            source = "rtf"
        elif clip.get("image") is not None:
            return ClipContent(kind="image", source_format="image")

        if text is None:
            return ClipContent(kind="empty")

        raw = text
        if cfg.normalize_whitespace:
            text = normalize_ws(text)

        # 空内容（纯空白）视同没有内容，不触发粘贴
        if not text.strip():
            return ClipContent(kind="empty")

        truncated = False
        if cfg.max_text_length and len(text) > cfg.max_text_length:
            text = text[: cfg.max_text_length]
            truncated = True

        return ClipContent(
            kind="text",
            text=text,
            raw=raw,
            source_format=source,
            trunclated=truncated,
            fingerprint=fingerprint(text),
        )

    # ------------------------------------------------------------------
    def is_duplicate(self, content: ClipContent) -> bool:
        """
        同内容在 dedupe_ttl 内重复出现 → 判为重复。
        注意：不更新 _last_time，否则持续轮询同一内容会永远"未过期"。
        """
        if not content:
            return True
        now = time.monotonic()
        if content.fingerprint == self._last_fp:
            if now - self._last_time < self.cfg.dedupe_ttl:
                return True
        return False

    def mark_seen(self, content: ClipContent) -> None:
        self._last_fp = content.fingerprint
        self._last_time = time.monotonic()

    def is_own_content(self, content: ClipContent) -> bool:
        """
        判断这份内容是不是我们自己刚写进去的。
        用于 restore_clipboard 场景，避免自己触发自己的粘贴。
        """
        return (
            content.fingerprint == self._last_fp
            and (time.monotonic() - self._last_time) < 0.5
        )
