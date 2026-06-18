"""trace_id / span_id 生成。

- trace_id：一次端到端 ROI 勘探请求，跨 11 服务。形如 tr_20260613T0103_a1f9c2。
- span_id ：一个决策点/阶段事件。形如 sp_1a2b3c4d5e6f。

均为字符串，可读、可排序（trace_id 前缀含 UTC 分钟时间戳，便于按时间归档与 bbox 兜底匹配）。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

TRACE_PREFIX = "tr_"
SPAN_PREFIX = "sp_"


def new_trace_id() -> str:
    """生成全局 trace_id：tr_<UTC YYYYMMDDTHHMM>_<6位hex>。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    return f"{TRACE_PREFIX}{stamp}_{secrets.token_hex(3)}"


def new_span_id() -> str:
    """生成事件级 span_id：sp_<12位hex>。"""
    return f"{SPAN_PREFIX}{secrets.token_hex(6)}"


def is_trace_id(value) -> bool:
    return isinstance(value, str) and value.startswith(TRACE_PREFIX)


def utc_now_iso() -> str:
    """统一 UTC 时间戳（秒级，带 Z）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
