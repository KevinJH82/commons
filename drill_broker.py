"""
drill_broker.py — 订阅 geo-drill 钻探验证与布孔闭环标准输出

纯文件系统订阅，只读，高失败容忍。布局：
    <results>/<AOI>/drill/<run_id>/metadata.json (+ planned_holes.geojson / drill_feedback.geojson / holes_db.json / figures)
下游（geo-reporter）注入「钻探验证与布孔建议」章节；drill_feedback 由 geo-model3d
（方向四 P4 的 load_drill_feedback）回灌为已知矿点(见矿)/真负样本(无矿)。
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_GEO_DRILL_OUTPUTS = "/opt/deepexplor-services/geo-drill/results"


def _load_metadata(run_dir: Path) -> Optional[Dict]:
    mp = run_dir / "metadata.json"
    if not mp.exists():
        return None
    try:
        with open(mp, "r", encoding="utf-8") as f:
            md = json.load(f)
        return md if md.get("source") == "geo-drill" else None
    except Exception:
        return None


def _resolve_product_dir(drill_dir: Path):
    if not drill_dir.is_dir():
        return None, None, 0
    runs = sorted((d for d in drill_dir.iterdir()
                   if d.is_dir() and (d / "metadata.json").exists()),
                  key=lambda d: d.name, reverse=True)
    if runs:
        return runs[0], _load_metadata(runs[0]), len(runs)
    return None, None, 0


def scan_drill_outputs(geo_drill_outputs: str = DEFAULT_GEO_DRILL_OUTPUTS) -> List[Dict]:
    root = Path(geo_drill_outputs)
    if not root.exists():
        return []
    out: List[Dict] = []
    for aoi_dir in root.iterdir():
        if not aoi_dir.is_dir() or aoi_dir.name.startswith("_"):
            continue
        run_dir, md, n_runs = _resolve_product_dir(aoi_dir / "drill")
        if md is None:
            continue
        out.append({
            "aoi_name": md.get("aoi_name") or aoi_dir.name,
            "aoi_bbox": md.get("aoi_bbox"),
            "crs": md.get("crs", "EPSG:4326"),
            "drill_dir": str(run_dir),
            "metadata_path": str(run_dir / "metadata.json"),
            "products": md.get("products", {}),
            "model_stats": md.get("model_stats", {}),
            "run_id": run_dir.name,
            "n_runs": n_runs,
            "trace_id": md.get("trace_id"),
            "linked_trace_ids": md.get("linked_trace_ids", []),
            "tenant_id": md.get("tenant_id"),
        })
    return out


def _bbox_intersects(a, b) -> bool:
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def find_drill_for_bbox(
    bbox: Tuple[float, float, float, float],
    geo_drill_outputs: str = DEFAULT_GEO_DRILL_OUTPUTS,
    trace_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> List[Dict]:
    """trace_id（可选）：优先按 trace_id 精确匹配，未命中回退 bbox（见架构蓝图 §1.3）。"""
    matches = [a for a in scan_drill_outputs(geo_drill_outputs)
               if _bbox_intersects(a.get("aoi_bbox"), bbox)]
    try:
        from commons.trace import filter_by_trace_id, filter_by_tenant
        return filter_by_trace_id(filter_by_tenant(matches, tenant_id), trace_id)
    except Exception:
        return matches


def get_product_path(entry: Dict, key: str) -> Optional[str]:
    rel = (entry.get("products") or {}).get(key)
    if not rel or not isinstance(rel, str):
        return None
    p = Path(entry["drill_dir"]) / rel
    return str(p) if p.exists() else None


def _load_points(entry: Dict, key: str) -> List[Dict]:
    p = get_product_path(entry, key)
    if not p:
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            fc = json.load(f)
        out = []
        for ft in fc.get("features", []):
            g = ft.get("geometry") or {}
            if g.get("type") != "Point" or len(g.get("coordinates", [])) < 2:
                continue
            c = g["coordinates"]
            pr = dict(ft.get("properties", {}) or {})
            pr.update({"lon": float(c[0]), "lat": float(c[1])})
            out.append(pr)
        return out
    except Exception:
        return []


def get_holes(entry: Dict) -> List[Dict]:
    """AI 计划孔 → [{rank,hole_id,lon,lat,target_depth_m,score,uncertainty,priority,...}]。"""
    return _load_points(entry, "planned_holes")


def get_feedback(entry: Dict) -> List[Dict]:
    """钻孔反馈 → [{hole_id,lon,lat,outcome(ore/barren),element,max_grade,cutoff}]。"""
    return _load_points(entry, "drill_feedback")
