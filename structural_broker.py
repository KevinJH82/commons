"""
structural_broker.py — 订阅 geo-stru 标准构造解译输出

与 insar_broker 同思路:纯文件系统订阅,零消息队列,高失败容忍。
geo-stru 产物布局:<results>/<AOI_NAME>/structural/metadata.json (+ 各 GeoTIFF/GeoJSON)。
下游(geo-analyser / geo-exploration / geo-reporter)通过本模块按 AOI 或 bbox 相交发现产物。

被各下游系统共用,因此放在 commons/。所有发现逻辑只读,不修改 geo-stru 的输出。
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_GEO_STRU_OUTPUTS = "/opt/deepexplor-services/geo-stru/results"


def _load_metadata(structural_dir: Path) -> Optional[Dict]:
    mp = structural_dir / "metadata.json"
    if not mp.exists():
        return None
    try:
        with open(mp, "r", encoding="utf-8") as f:
            md = json.load(f)
        if not (md.get("source") or "").startswith("geo-stru"):
            return None
        return md
    except Exception:
        return None


def _resolve_product_dir(structural_dir: Path):
    """
    定位 AOI 下实际产品目录,兼容两种布局:
      - 版本化(当前):structural/<run_id>/metadata.json,取最新 run(按目录名时间戳降序)。
      - 扁平(历史):structural/metadata.json。
    返回 (product_dir, metadata, n_runs);找不到返回 (None, None, 0)。
    """
    if not structural_dir.is_dir():
        return None, None, 0
    # 优先版本化布局:含 metadata.json 的 run 子目录,取最新(时间戳降序)
    runs = sorted((d for d in structural_dir.iterdir()
                   if d.is_dir() and (d / "metadata.json").exists()),
                  key=lambda d: d.name, reverse=True)
    if runs:
        latest = runs[0]
        return latest, _load_metadata(latest), len(runs)
    # 回退扁平历史布局:structural/metadata.json
    flat = _load_metadata(structural_dir)
    if flat is not None:
        return structural_dir, flat, 1
    return None, None, 0


def scan_structural_aois(geo_stru_outputs: str = DEFAULT_GEO_STRU_OUTPUTS) -> List[Dict]:
    """
    扫描 geo-stru 输出,返回每个 AOI 的最新构造解译产物(保留历史 run,默认取最新)。

    Returns
    -------
    [{aoi_name, aoi_bbox, crs, structural_dir, metadata_path, products,
      structural_stats, deposit_inference, run_id, n_runs}, ...]
    """
    root = Path(geo_stru_outputs)
    if not root.exists():
        return []

    out = []
    for aoi_dir in root.iterdir():
        if not aoi_dir.is_dir() or aoi_dir.name.startswith("_"):
            continue
        product_dir, md, n_runs = _resolve_product_dir(aoi_dir / "structural")
        if md is None:
            continue
        out.append({
            "aoi_name": md.get("aoi_name") or aoi_dir.name,
            "aoi_bbox": md.get("aoi_bbox"),
            "crs": md.get("crs", "EPSG:4326"),
            "structural_dir": str(product_dir),
            "metadata_path": str(product_dir / "metadata.json"),
            "products": md.get("products", {}),
            "structural_stats": md.get("structural_stats", {}),
            "deposit_inference": md.get("deposit_inference"),
            "run_id": md.get("run_id"),
            "n_runs": n_runs,
            "trace_id": md.get("trace_id"),
            "linked_trace_ids": md.get("linked_trace_ids", []),
            "tenant_id": md.get("tenant_id"),
        })
    return out


def list_structural_runs(aoi_name: str,
                         geo_stru_outputs: str = DEFAULT_GEO_STRU_OUTPUTS) -> List[Dict]:
    """列出某 AOI 的所有历史 run(最新在前),供下游做时序/版本对比。"""
    sdir = Path(geo_stru_outputs) / aoi_name / "structural"
    if not sdir.is_dir():
        return []
    runs = []
    # 扁平历史布局
    flat = _load_metadata(sdir)
    if flat is not None:
        runs.append({"run_id": flat.get("run_id"), "structural_dir": str(sdir),
                     "created_at": flat.get("created_at"),
                     "structural_stats": flat.get("structural_stats", {})})
    for d in sorted((d for d in sdir.iterdir()
                     if d.is_dir() and (d / "metadata.json").exists()),
                    key=lambda d: d.name, reverse=True):
        m = _load_metadata(d)
        if m is not None:
            runs.append({"run_id": m.get("run_id") or d.name, "structural_dir": str(d),
                         "created_at": m.get("created_at"),
                         "structural_stats": m.get("structural_stats", {})})
    return runs


def _bbox_intersects(a, b) -> bool:
    """两个 [min_lon,min_lat,max_lon,max_lat] 是否相交。"""
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def find_structural_for_bbox(
    bbox: Tuple[float, float, float, float],
    geo_stru_outputs: str = DEFAULT_GEO_STRU_OUTPUTS,
    trace_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> List[Dict]:
    """返回与给定 bbox 相交的所有 geo-stru 产物(供 reporter/exploration/analyser 按研究区匹配)。

    trace_id（可选）：优先按 trace_id 精确匹配，未命中回退 bbox（见架构蓝图 §1.3）。
    """
    matches = [a for a in scan_structural_aois(geo_stru_outputs)
               if _bbox_intersects(a.get("aoi_bbox"), bbox)]
    try:
        from commons.trace import filter_by_trace_id, filter_by_tenant
        return filter_by_trace_id(filter_by_tenant(matches, tenant_id), trace_id)
    except Exception:
        return matches


def get_product_path(entry: Dict, key: str) -> Optional[str]:
    """从 scan 结果项取某产品(如 'distance_to_lineament')的绝对路径,不存在返回 None。"""
    rel = (entry.get("products") or {}).get(key)
    if not rel:
        return None
    p = Path(entry["structural_dir"]) / rel
    return str(p) if p.exists() else None
