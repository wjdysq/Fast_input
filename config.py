# -*- coding: utf-8 -*-
"""
配置层：把"粘到哪""什么时候粘""怎么粘"从代码里抽出来，落到 JSON 文件。

设计原则：
- 代码只认 config 对象，不认命令行细节，方便以后加 GUI 设置面板。
- 所有字段都有默认值，缺字段不会崩。
- 未知字段会被保留（向后兼容），只做类型校验。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any


# --------------------------------------------------------------------------
# 默认配置
# --------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".clip_autopaste.json")


DEFAULTS: dict[str, Any] = {
    # ---- 监听 ----
    "poll_interval": 0.25,        # 秒。剪贴板轮询间隔（只在没有原生事件时用）
    "use_native_events": True,    # Windows 上优先用 AddClipboardFormatListener
    "dedupe_ttl": 2.0,            # 秒。同一内容在该时间窗内重复出现只算一次
    "max_text_length": 0,         # 0 = 不限。超过该长度的文本拒绝处理（防误粘大文件）

    # ---- 格式 ----
    "prefer_format": "text",      # text | html | rtf | image
    "strip_html": True,           # 目标不支持富文本时，把 HTML 降级为纯文本
    "normalize_whitespace": False,  # 是否把多余空行/行尾空格压掉
    "image_action": "skip",       # skip | save。图片内容怎么处理

    # ---- 触发 ----
    "trigger_mode": "hotkey",     # hotkey | auto | delay
    "hotkey": "ctrl+alt+v",       # 仅 trigger_mode=hotkey 时生效
    "delay_seconds": 1.5,         # 仅 trigger_mode=delay 时生效

    # ---- 输入手法 ----
    # paste : 发 Ctrl+V，瞬间完成（快，但有些控件不响应）
    # type  : 逐字符快速键入，形成连续输入效果（默认）
    "paste_method": "type",
    "type_interval": 0.005,       # 秒/字符。越小越快；0 = 不限速
    "type_show_progress": True,   # 长文本时在日志里显示进度
    "type_progress_step": 50,     # 每输入多少字符报一次进度

    # ---- 目标 ----
    "target_mode": "foreground",  # foreground | title | handle
    "target_title": "",           # 仅 target_mode=title 时生效，支持部分匹配（不区分大小写）
    "target_handle": 0,           # 仅 target_mode=handle 时生效
    "restore_clipboard": False,   # 粘贴后是否把剪贴板还原成粘贴前的内容

    # ---- 安全 ----
    "exclude_titles": [],         # 命中这些标题（子串匹配）的窗口一律不注入，防止粘到密码框
    "require_confirm": False,     # True 时每次粘贴前在终端确认（调试用）

    # ---- 运行 ----
    "log_level": "INFO",          # DEBUG | INFO | WARNING | ERROR
    "dry_run": False,             # True 时只打印"我本会粘什么"，不真的注入
}


# --------------------------------------------------------------------------
# 配置对象
# --------------------------------------------------------------------------

@dataclass
class Config:
    poll_interval: float = 0.25
    use_native_events: bool = True
    dedupe_ttl: float = 2.0
    max_text_length: int = 0
    prefer_format: str = "text"
    strip_html: bool = True
    normalize_whitespace: bool = False
    image_action: str = "skip"
    trigger_mode: str = "hotkey"
    hotkey: str = "ctrl+alt+v"
    delay_seconds: float = 1.5
    paste_method: str = "type"
    type_interval: float = 0.005
    type_show_progress: bool = True
    type_progress_step: int = 50
    target_mode: str = "foreground"
    target_title: str = ""
    target_handle: int = 0
    restore_clipboard: bool = False
    exclude_titles: list = field(default_factory=list)
    require_confirm: bool = False
    log_level: str = "INFO"
    dry_run: bool = False

    # 保留用户 JSON 里我们不认识的键，保存时不丢
    _extra: dict = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def validate(self) -> list[str]:
        """返回问题列表。空列表 = 配置合法。"""
        problems: list[str] = []

        if self.poll_interval <= 0.02:
            problems.append("poll_interval 太小（<=0.02s），会明显吃 CPU，建议 0.1~0.5")
        if self.paste_method == "type" and 0 < self.type_interval < 0.001:
            problems.append(
                f"type_interval={self.type_interval} 过小，目标应用可能来不及处理导致丢字，"
                "建议 >=0.002（想最快可以用 0，走不限速模式）"
            )
        if self.trigger_mode not in ("hotkey", "auto", "delay"):
            problems.append(f"trigger_mode 非法: {self.trigger_mode}")
        if self.target_mode not in ("foreground", "title", "handle"):
            problems.append(f"target_mode 非法: {self.target_mode}")
        if self.paste_method not in ("paste", "type"):
            problems.append(f"paste_method 非法: {self.paste_method}（应为 paste 或 type）")
        if self.type_interval < 0:
            problems.append("type_interval 不能为负")
        if self.prefer_format not in ("text", "html", "rtf", "image"):
            problems.append(f"prefer_format 非法: {self.prefer_format}")
        if self.image_action not in ("skip", "save"):
            problems.append(f"image_action 非法: {self.image_action}")

        if self.target_mode == "title" and not self.target_title:
            problems.append("target_mode=title 但没有填 target_title")
        if self.target_mode == "handle" and not self.target_handle:
            problems.append("target_mode=handle 但没有填 target_handle")
        if self.trigger_mode == "hotkey" and not self.hotkey:
            problems.append("trigger_mode=hotkey 但没有填 hotkey")

        return problems

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_extra", None)
        d.update(self._extra)
        return d

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or DEFAULT_CONFIG_PATH

        merged = dict(DEFAULTS)
        extra: dict = {}

        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    user = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                raise ValueError(f"配置文件读取失败 {path}: {e}") from e

            if not isinstance(user, dict):
                raise ValueError(f"配置文件根节点必须是对象，实际是 {type(user).__name__}")

            known = set(DEFAULTS)
            for k, v in user.items():
                if k in known:
                    merged[k] = v
                else:
                    extra[k] = v

        cfg = cls(**merged)
        cfg._extra = extra
        return cfg

    def save(self, path: str | None = None) -> str:
        path = path or DEFAULT_CONFIG_PATH
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 原子替换，避免写一半被读到
        return path


def write_default_config(path: str | None = None, overwrite: bool = False) -> str:
    """生成一份带注释说明的默认配置模板（JSON 不支持注释，另存 .md 说明）。"""
    path = path or DEFAULT_CONFIG_PATH
    if os.path.isfile(path) and not overwrite:
        return path
    Config().save(path)
    return path
