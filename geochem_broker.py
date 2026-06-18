"""
geochem_broker.py — 订阅 geo-geochem 地球化学异常处理标准输出

纯文件系统订阅，只读，高失败容忍。布局：
    <results>/<AOI>/geochem/<run_id>/metadata.json (+ grids/*.tif, anomalies.geojson, figures/*.png)
下游（geo-model3d / geo-reporter）按 AOI 或 bbox 相交发现产物：
  - geo-model3d 取多元素组合异常 + 浓集中心，作地表证据层（与遥感蚀变联合，压假阳性）。
  - geo-reporter 取异常图件，注入「地球化学异常」章节。
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_GEO_GEOCHEM_OUTPUTS = "/opt/deepexplor-services/geo-geochem/results"


def _load_metadata(run_dir: Path) -> Optional[Dict]:
    mp = run_dir / "metadata.json"
    if not mp.exists():
        return None
    try:
        with open(mp, "r", encoding="utf-8") as f:
            md = json.load(f)
        return md if md.get("source") == "geo-geochem" else None
    except Exception:
        return None


def _resolve_product_dir(geochem_dir: Path):
    if not geochem_dir.is_dir():
        return None, None, 0
    runs = sorted((d for d in geochem_dir.iterdir()
                   if d.is_dir() and (d / "metadata.json").exists()),
                  key=lambda d: d.name, reverse=True)
    if runs:
        return runs[0], _load_metadata(runs[0]), len(runs)
    return None, None, 0


def scan_geochem_outputs(geo_geochem_outputs: str = DEFAULT_GEO_GEOCHEM_OUTPUTS) -> List[Dict]:
    root = Path(geo_geochem_outputs)
    if not root.exists():
        return []
    out: List[Dict] = []
    for aoi_dir in root.iterdir():
        if not aoi_dir.is_dir() or aoi_dir.name.startswith("_"):
            continue
        run_dir, md, n_runs = _resolve_product_dir(aoi_dir / "geochem")
        if md is None:
            continue
        out.append({
            "aoi_name": md.get("aoi_name") or aoi_dir.name,
            "aoi_bbox": md.get("aoi_bbox"),
            "crs": md.get("crs", "EPSG:4326"),
            "geochem_dir": str(run_dir),
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


def list_geochem_runs(aoi_name: str,
                      geo_geochem_outputs: str = DEFAULT_GEO_GEOCHEM_OUTPUTS) -> List[Dict]:
    gdir = Path(geo_geochem_outputs) / aoi_name / "geochem"
    if not gdir.is_dir():
        return []
    runs = []
    for d in sorted((d for d in gdir.iterdir()
                     if d.is_dir() and (d / "metadata.json").exists()),
                    key=lambda d: d.name, reverse=True):
        m = _load_metadata(d)
        if m is not None:
            runs.append({"run_id": d.name, "geochem_dir": str(d),
                         "created_at": m.get("created_at"),
                         "model_stats": m.get("model_stats", {})})
    return runs


def _bbox_intersects(a, b) -> bool:
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def find_geochem_for_bbox(
    bbox: Tuple[float, float, float, float],
    geo_geochem_outputs: str = DEFAULT_GEO_GEOCHEM_OUTPUTS,
    trace_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> List[Dict]:
    """trace_id（可选）：优先按 trace_id 精确匹配，未命中回退 bbox（见架构蓝图 §1.3）。"""
    matches = [a for a in scan_geochem_outputs(geo_geochem_outputs)
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
    p = Path(entry["geochem_dir"]) / rel
    return str(p) if p.exists() else None


def load_anomaly_points(entry: Dict) -> List[Dict]:
    """读组合异常浓集中心 → [{lon,lat,peak_intensity,mean_intensity,area_km2,contrast,rank}]，无则空。"""
    p = get_product_path(entry, "anomalies")
    if not p:
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            fc = json.load(f)
        out = []
        for ft in fc.get("features", []):
            pr = ft.get("properties", {})
            out.append({"lon": pr.get("lon"), "lat": pr.get("lat"),
                        "peak_intensity": pr.get("peak_intensity"),
                        "mean_intensity": pr.get("mean_intensity"),
                        "area_km2": pr.get("area_km2"), "contrast": pr.get("contrast"),
                        "rank": pr.get("rank")})
        return out
    except Exception:
        return []
