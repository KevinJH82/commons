"""trace_id 血缘传播工具 —— 服务写盘与 broker 发现共用的一套逻辑。

服务写 metadata 时调 stamp_metadata() 决定本次运行的 trace_id：
  1) 调用方显式给定        → trace_origin=explicit（P2 orchestrator 注入 / 前端透传）
  2) 否则从上游 metadata 继承 → trace_origin=inherited（P1 核心：沿数据血缘）
  3) 都没有则自生成        → trace_origin=self（单服务独立跑兜底）

broker 发现时调 filter_by_trace_id() 做 trace_id 优先匹配，未命中回退 bbox（在 broker 内）。

这套逻辑只读/纯函数，无 I/O，容错由调用方负责（写 metadata 本就在 try 内）。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .ids import new_trace_id
from .schema import ORIGIN_EXPLICIT, ORIGIN_INHERITED, ORIGIN_SELF


def resolve_trace_id(explicit_trace_id: Optional[str] = None,
                     upstream_metadatas: Optional[List[Dict]] = None
                     ) -> Tuple[str, List[str], str]:
    """推导本次运行的 (trace_id, linked_trace_ids, trace_origin)。

    upstream_metadatas 建议按 created_at 降序传入，则继承时天然"最近优先"。
    """
    if explicit_trace_id:
        return explicit_trace_id, [explicit_trace_id], ORIGIN_EXPLICIT

    linked: List[str] = []
    for md in upstream_metadatas or []:
        tid = (md or {}).get("trace_id")
        if tid and tid not in linked:
            linked.append(tid)
    if linked:
        return linked[0], linked, ORIGIN_INHERITED

    new_tid = new_trace_id()
    return new_tid, [new_tid], ORIGIN_SELF


def stamp_metadata(meta: Dict, *, explicit_trace_id: Optional[str] = None,
                   upstream_metadatas: Optional[List[Dict]] = None,
                   tenant_id: Optional[str] = None) -> str:
    """就地给 metadata dict 注入 trace_id/linked_trace_ids/trace_origin(+可选 tenant_id)，返回 trace_id。

    幂等：若 meta 已含非空 trace_id 则尊重之，仅补全缺失键。
    tenant_id（P2 租户隔离）：给定且 meta 未含 tenant_id 时写入，供 broker 发现按租户过滤。
    """
    if tenant_id and not meta.get("tenant_id"):
        meta["tenant_id"] = tenant_id

    existing = meta.get("trace_id")
    if existing:
        meta.setdefault("linked_trace_ids", [existing])
        meta.setdefault("trace_origin", meta.get("trace_origin", ORIGIN_EXPLICIT))
        return existing

    tid, linked, origin = resolve_trace_id(explicit_trace_id, upstream_metadatas)
    meta["trace_id"] = tid
    meta["linked_trace_ids"] = linked
    meta["trace_origin"] = origin
    return tid


def entry_matches_trace(entry: Dict, trace_id: str) -> bool:
    """broker 返回项是否属于给定 trace_id（命中主 trace 或 linked 之一）。"""
    if not trace_id or not isinstance(entry, dict):
        return False
    if entry.get("trace_id") == trace_id:
        return True
    return trace_id in (entry.get("linked_trace_ids") or [])


def filter_by_trace_id(entries: List[Dict], trace_id: Optional[str]) -> List[Dict]:
    """trace_id 优先筛选；trace_id 为空时原样返回（交回 broker 走 bbox 兜底）。

    注意：跨租户隔离由 filter_by_tenant 在本函数之前完成（broker 内先按 tenant 收窄），
    故此处的 bbox 兜底结果已是租户安全的，未命中回退不会泄露他租户产物。
    """
    if not trace_id:
        return entries
    hits = [e for e in entries if entry_matches_trace(e, trace_id)]
    return hits if hits else entries  # 未命中则不收窄，保留(已租户过滤的)bbox 兜底结果


def entry_matches_tenant(entry: Dict, tenant_id: str) -> bool:
    """产物是否属于给定租户。

    租户隔离规则(向后兼容):
    - 调用方无 tenant_id(None) → 不过滤,放行(单服务独立跑/未启用多租户);
    - 产物 metadata 无 tenant_id(历史未打标) → 放行(单租户时代遗留,由迁移脚本另行归属);
    - 两者都有 → 必须相等。
    """
    if not tenant_id:
        return True
    et = (entry or {}).get("tenant_id")
    if not et:
        return True
    return et == tenant_id


def filter_by_tenant(entries: List[Dict], tenant_id: Optional[str]) -> List[Dict]:
    """按租户收窄 broker 发现结果:丢弃明确属于他租户的产物。bbox/trace 筛选之前调用。"""
    if not tenant_id:
        return entries
    return [e for e in entries if entry_matches_tenant(e, tenant_id)]
