"""
insar_broker.py — 订阅 geo-insar 标准输出（平台唯一权威 broker）

纯文件系统订阅，只读，高失败容忍。布局：
    <downloads>/<AOI>/sentinel1_insar/<pair>/metadata.json   逐对干涉产物（schema 见 insar_schema.json）
    <downloads>/<AOI>/sbas/<burst>/velocity_mm_per_year.tif   SBAS 逐 burst 速率
    <downloads>/<AOI>/deformation_evidence.tif                AOI 级形变证据栅格（|velocity| 镶嵌）
    <downloads>/<AOI>/insar_metadata.json                     AOI 级平台契约（source/aoi_bbox/products/stats）
    <downloads>/<AOI>/stack_index.json                        （可选）干涉对堆栈汇总

下游消费：
  - geo-model3d 按 bbox 相交取 deformation_evidence 栅格，作第 6 层地表形变证据。
  - geo-analyser 用 scan_available_aois / get_stack_path 列出可时序反演的堆栈。
  - geo-reporter 走本地直读（fetch_insar_local）注入「InSAR 监测」章节。
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_GEO_INSAR_OUTPUTS = "/opt/deepexplor-services/geo-insar/downloads"


# ─────────────────────────────────────────────
# 新契约 API：AOI 级形变证据（供 geo-model3d）
# ─────────────────────────────────────────────
def _load_metadata(aoi_dir: Path) -> Optional[Dict]:
    """读 AOI 级 insar_metadata.json，校验 source == 'geo-insar'。"""
    mp = aoi_dir / "insar_metadata.json"
    if not mp.exists():
        return None
    try:
        with open(mp, "r", encoding="utf-8") as f:
            md = json.load(f)
        return md if md.get("source") == "geo-insar" else None
    except Exception:
        return None


def scan_insar_outputs(geo_insar_outputs: str = DEFAULT_GEO_INSAR_OUTPUTS) -> List[Dict]:
    """扫描所有 AOI，返回带 AOI 级形变证据契约的产物条目。"""
    root = Path(geo_insar_outputs)
    if not root.exists():
        return []
    out: List[Dict] = []
    for aoi_dir in root.iterdir():
        if not aoi_dir.is_dir() or aoi_dir.name.startswith("_"):
            continue
        md = _load_metadata(aoi_dir)
        if md is None:
            continue
        out.append({
            "aoi_name": md.get("aoi_name") or aoi_dir.name,
            "aoi_bbox": md.get("aoi_bbox"),
            "crs": md.get("crs", "EPSG:4326"),
            "insar_dir": str(aoi_dir),
            "metadata_path": str(aoi_dir / "insar_metadata.json"),
            "products": md.get("products", {}),
            "stats": md.get("stats", {}),
            "created_at": md.get("created_at"),
            "trace_id": md.get("trace_id"),
            "linked_trace_ids": md.get("linked_trace_ids", []),
            "tenant_id": md.get("tenant_id"),
        })
    return out


def _bbox_intersects(a, b) -> bool:
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def find_insar_for_bbox(
    bbox: Tuple[float, float, float, float],
    geo_insar_outputs: str = DEFAULT_GEO_INSAR_OUTPUTS,
    trace_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> List[Dict]:
    """返回与给定 bbox 相交的所有 geo-insar 形变证据产物。

    trace_id（可选）：优先按 trace_id 精确匹配，未命中回退 bbox（见架构蓝图 §1.3）。
    """
    matches = [a for a in scan_insar_outputs(geo_insar_outputs)
               if _bbox_intersects(a.get("aoi_bbox"), bbox)]
    try:
        from commons.trace import filter_by_trace_id, filter_by_tenant
        return filter_by_trace_id(filter_by_tenant(matches, tenant_id), trace_id)
    except Exception:
        return matches


def get_product_path(entry: Dict, key: str) -> Optional[str]:
    """从 scan 条目取某产品（如 deformation_evidence）的绝对路径，不存在返回 None。"""
    rel = (entry.get("products") or {}).get(key)
    if not rel or not isinstance(rel, str):
        return None
    p = Path(entry["insar_dir"]) / rel
    return str(p) if p.exists() else None


# ─────────────────────────────────────────────
# 兼容旧 API（Phase 1.5，供 geo-analyser）：基于 per-pair 堆栈
# ─────────────────────────────────────────────
def scan_available_aois(geo_insar_outputs: str = DEFAULT_GEO_INSAR_OUTPUTS) -> List[Dict]:
    """
    扫描 geo-insar 输出目录,返回所有可分析的 AOI 列表。

    Returns
    -------
    [{aoi_name, aoi_path, n_pairs, date_range, stack_index_path}, ...]
    """
    root = Path(geo_insar_outputs)
    if not root.exists():
        return []

    out = []
    for aoi_dir in root.iterdir():
        if not aoi_dir.is_dir():
            continue
        pair_dirs = sorted(aoi_dir.glob("sentinel1_insar/*"))
        pair_dirs = [p for p in pair_dirs if p.is_dir() and (p / "metadata.json").exists()]
        if not pair_dirs:
            continue

        # 优先用 stack_index.json(汇总),fallback 到逐个 pair
        idx = aoi_dir / "stack_index.json"
        if idx.exists():
            try:
                with open(idx, "r", encoding="utf-8") as f:
                    summary = json.load(f)
                out.append({
                    "aoi_name": aoi_dir.name,
                    "aoi_path": str(aoi_dir),
                    "n_pairs": summary.get("pair_count", len(pair_dirs)),
                    "date_range": summary.get("date_range", [None, None]),
                    "stack_index_path": str(idx),
                })
                continue
            except Exception:
                pass

        # fallback
        dates = []
        for pdir in pair_dirs:
            try:
                with open(pdir / "metadata.json", "r", encoding="utf-8") as f:
                    m = json.load(f)
                dates.extend([m.get("master_date"), m.get("slave_date")])
            except Exception:
                pass
        dates = sorted(d for d in dates if d)
        out.append({
            "aoi_name": aoi_dir.name,
            "aoi_path": str(aoi_dir),
            "n_pairs": len(pair_dirs),
            "date_range": [dates[0], dates[-1]] if dates else [None, None],
            "stack_index_path": None,
        })
    return out


def get_stack_path(aoi_name: str,
                   geo_insar_outputs: str = DEFAULT_GEO_INSAR_OUTPUTS) -> Optional[str]:
    """根据 aoi_name 返回对应 AOI 目录路径(供 insar_timeseries.load_insar_stack 用)。"""
    aoi_dir = Path(geo_insar_outputs) / aoi_name
    if aoi_dir.is_dir():
        return str(aoi_dir)
    return None
