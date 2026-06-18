"""
model3d_broker.py — 订阅 geo-model3d 三维立体成矿预测标准输出

与 structural_broker 同思路：纯文件系统订阅，只读，高失败容忍。
geo-model3d 产物布局：<results>/<AOI>/model3d/<run_id>/metadata.json (+ NetCDF体/深度切片GeoTIFF/PNG/targets_3d.json)。
下游（geo-reporter）按 AOI 或 bbox 相交发现产物，注入「三维成矿预测」章节。

被各下游系统共用，放在 commons/。所有发现逻辑只读，不修改 geo-model3d 输出。
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_GEO_MODEL3D_OUTPUTS = "/opt/deepexplor-services/geo-model3d/results"


def _load_metadata(run_dir: Path) -> Optional[Dict]:
    mp = run_dir / "metadata.json"
    if not mp.exists():
        return None
    try:
        with open(mp, "r", encoding="utf-8") as f:
            md = json.load(f)
        if md.get("source") != "geo-model3d":
            return None
        return md
    except Exception:
        return None


def _resolve_product_dir(model3d_dir: Path):
    """定位 AOI/model3d 下最新 run（按目录名时间戳降序）。返回 (run_dir, metadata, n_runs)。"""
    if not model3d_dir.is_dir():
        return None, None, 0
    runs = sorted((d for d in model3d_dir.iterdir()
                   if d.is_dir() and (d / "metadata.json").exists()),
                  key=lambda d: d.name, reverse=True)
    if runs:
        latest = runs[0]
        return latest, _load_metadata(latest), len(runs)
    return None, None, 0


def scan_model3d_outputs(geo_model3d_outputs: str = DEFAULT_GEO_MODEL3D_OUTPUTS) -> List[Dict]:
    """
    扫描 geo-model3d 输出，返回每个 AOI 的最新三维建模产物。

    Returns
    -------
    [{aoi_name, aoi_bbox, crs, model3d_dir, metadata_path, products,
      model_stats, run_id, n_runs}, ...]
    """
    root = Path(geo_model3d_outputs)
    if not root.exists():
        return []

    out: List[Dict] = []
    for aoi_dir in root.iterdir():
        if not aoi_dir.is_dir() or aoi_dir.name.startswith("_"):
            continue
        run_dir, md, n_runs = _resolve_product_dir(aoi_dir / "model3d")
        if md is None:
            continue
        out.append({
            "aoi_name": md.get("aoi_name") or aoi_dir.name,
            "aoi_bbox": md.get("aoi_bbox"),
            "crs": md.get("crs", "EPSG:4326"),
            "model3d_dir": str(run_dir),
            "metadata_path": str(run_dir / "metadata.json"),
            "products": md.get("products", {}),
            "model_stats": md.get("model_stats", {}),
            "run_id": Path(md.get("metadata_path", "")).parent.name if md.get("metadata_path") else run_dir.name,
            "n_runs": n_runs,
            "trace_id": md.get("trace_id"),
            "linked_trace_ids": md.get("linked_trace_ids", []),
            "tenant_id": md.get("tenant_id"),
        })
    return out


def list_model3d_runs(aoi_name: str,
                      geo_model3d_outputs: str = DEFAULT_GEO_MODEL3D_OUTPUTS) -> List[Dict]:
    """列出某 AOI 的所有历史 run（最新在前），供下游做版本对比。"""
    mdir = Path(geo_model3d_outputs) / aoi_name / "model3d"
    if not mdir.is_dir():
        return []
    runs = []
    for d in sorted((d for d in mdir.iterdir()
                     if d.is_dir() and (d / "metadata.json").exists()),
                    key=lambda d: d.name, reverse=True):
        m = _load_metadata(d)
        if m is not None:
            runs.append({"run_id": d.name, "model3d_dir": str(d),
                         "created_at": m.get("created_at"),
                         "model_stats": m.get("model_stats", {})})
    return runs


def _bbox_intersects(a, b) -> bool:
    """两个 [min_lon,min_lat,max_lon,max_lat] 是否相交。"""
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def find_model3d_for_bbox(
    bbox: Tuple[float, float, float, float],
    geo_model3d_outputs: str = DEFAULT_GEO_MODEL3D_OUTPUTS,
    trace_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> List[Dict]:
    """返回与给定 bbox 相交的所有 geo-model3d 产物（供 reporter 按研究区匹配）。

    trace_id（可选）：优先按 trace_id 精确匹配（消除 bbox 歧义，见架构蓝图 §1.3）；
    未命中或未提供则回退 bbox 相交。
    """
    matches = [a for a in scan_model3d_outputs(geo_model3d_outputs)
               if _bbox_intersects(a.get("aoi_bbox"), bbox)]
    try:
        from commons.trace import filter_by_trace_id, filter_by_tenant
        return filter_by_trace_id(filter_by_tenant(matches, tenant_id), trace_id)
    except Exception:
        return matches


def get_product_path(entry: Dict, key: str) -> Optional[str]:
    """从 scan 结果项取某产品（如 'depth_profile_png'）的绝对路径，不存在返回 None。"""
    rel = (entry.get("products") or {}).get(key)
    if not rel or not isinstance(rel, str):
        return None
    p = Path(entry["model3d_dir"]) / rel
    return str(p) if p.exists() else None
