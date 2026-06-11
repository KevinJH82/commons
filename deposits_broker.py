"""
deposits_broker.py — 订阅 geo-model3d 落地的已知矿点标签层（方向四）

纯文件系统订阅，只读，高失败容忍。布局：
    <results>/<AOI>/deposits/<run_id>/known_deposits.geojson (+ metadata.json, source=="geo-deposits")
geo-model3d 每次建模会把"本次用到的真实已知矿点"写为产物（来自 USGS MRDS + 用户上传，
**不含任何预测靶点**）。下游（geo-reporter / 复训）可据此发现并复用同一标签集，保证可追溯。
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_DEPOSITS_OUTPUTS = "/opt/deepexplor-services/geo-model3d/results"


def _load_metadata(run_dir: Path) -> Optional[Dict]:
    mp = run_dir / "metadata.json"
    if not mp.exists():
        return None
    try:
        with open(mp, "r", encoding="utf-8") as f:
            md = json.load(f)
        return md if md.get("source") == "geo-deposits" else None
    except Exception:
        return None


def _resolve_product_dir(deposits_dir: Path):
    if not deposits_dir.is_dir():
        return None, None, 0
    runs = sorted((d for d in deposits_dir.iterdir()
                   if d.is_dir() and (d / "metadata.json").exists()),
                  key=lambda d: d.name, reverse=True)
    if runs:
        return runs[0], _load_metadata(runs[0]), len(runs)
    return None, None, 0


def scan_deposits_outputs(deposits_outputs: str = DEFAULT_DEPOSITS_OUTPUTS) -> List[Dict]:
    root = Path(deposits_outputs)
    if not root.exists():
        return []
    out: List[Dict] = []
    for aoi_dir in root.iterdir():
        if not aoi_dir.is_dir() or aoi_dir.name.startswith("_"):
            continue
        run_dir, md, n_runs = _resolve_product_dir(aoi_dir / "deposits")
        if md is None:
            continue
        out.append({
            "aoi_name": md.get("aoi_name") or aoi_dir.name,
            "aoi_bbox": md.get("aoi_bbox"),
            "crs": md.get("crs", "EPSG:4326"),
            "deposits_dir": str(run_dir),
            "metadata_path": str(run_dir / "metadata.json"),
            "products": md.get("products", {}),
            "label_status": md.get("label_status", {}),
            "run_id": run_dir.name,
            "n_runs": n_runs,
        })
    return out


def _bbox_intersects(a, b) -> bool:
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def find_deposits_for_bbox(
    bbox: Tuple[float, float, float, float],
    deposits_outputs: str = DEFAULT_DEPOSITS_OUTPUTS,
) -> List[Dict]:
    return [a for a in scan_deposits_outputs(deposits_outputs)
            if _bbox_intersects(a.get("aoi_bbox"), bbox)]


def get_product_path(entry: Dict, key: str) -> Optional[str]:
    rel = (entry.get("products") or {}).get(key)
    if not rel or not isinstance(rel, str):
        return None
    p = Path(entry["deposits_dir"]) / rel
    return str(p) if p.exists() else None


def get_points(entry: Dict) -> List[Dict]:
    """读已知矿点 → [{lon,lat,commodity,deposit_type,name,source}]，无则空。"""
    p = get_product_path(entry, "known_deposits")
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
            pr = ft.get("properties", {}) or {}
            out.append({"lon": float(c[0]), "lat": float(c[1]),
                        "commodity": pr.get("commodity", ""),
                        "deposit_type": pr.get("deposit_type", ""),
                        "name": pr.get("name", ""), "source": pr.get("source", "")})
        return out
    except Exception:
        return []
