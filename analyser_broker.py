"""
analyser_broker.py — 订阅 geo-analyser 标准蚀变分析输出

纯文件系统订阅，只读，高失败容忍。geo-analyser 产物布局：
    <results>/<AOI>/latest.json                         （deposit_type → 最新 run 相对路径）
    <results>/<AOI>/<deposit>_<时间戳>/manifest.json     （蚀变分析清单）
        ├── roi_geojson           （ROI 多边形，用于计算 bbox）
        ├── results[]             （各矿物/方法：anomaly_ratio / preview_png ...）
        ├── composites{sensor}    （多矿物交集/评分图）
        └── structural            （构造-蚀变空间关联，可选）

下游（geo-reporter）按 bbox 相交发现匹配本研究区的蚀变分析，注入「遥感影像」章节，
作为本 AOI 影像实测的硬本地实证（优先于 Web 搜索）。
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_GEO_ANALYSER_OUTPUTS = "/opt/deepexplor-services/geo-analyser/results"


def _bbox_intersects(a, b) -> bool:
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _read_json(p: Path) -> Optional[dict]:
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _bbox_from_geojson(roi_geojson: dict) -> Optional[List[float]]:
    try:
        ring = roi_geojson["coordinates"][0]
        lons = [pt[0] for pt in ring]
        lats = [pt[1] for pt in ring]
        return [min(lons), min(lats), max(lons), max(lats)]
    except Exception:
        return None


def _abs(run_dir: Path, rel: Optional[str]) -> Optional[str]:
    if not rel:
        return None
    p = run_dir / rel
    return str(p) if p.exists() else None


def _entry_from_manifest(run_dir: Path, md: dict) -> Optional[Dict]:
    bbox = _bbox_from_geojson(md.get("roi_geojson") or {})
    if bbox is None:
        return None

    results = md.get("results", []) or []
    figures: List[Dict] = []
    minerals_seen = set()
    for r in results:
        mineral = r.get("mineral", "")
        png = _abs(run_dir, r.get("preview_png"))
        if png and mineral not in minerals_seen:
            figures.append({
                "path": png,
                "caption": f"{mineral} 蚀变异常（{r.get('sensor','')}/{r.get('method','')}，"
                           f"异常占比 {r.get('anomaly_ratio','?')}%）",
                "source": "geo-analyser",
            })
            minerals_seen.add(mineral)

    # 多矿物综合图（每个传感器取评分图）
    for sensor, comp in (md.get("composites") or {}).items():
        png = _abs(run_dir, comp.get("score_png")) or _abs(run_dir, comp.get("intersection_png"))
        if png:
            figures.append({
                "path": png,
                "caption": f"{sensor} 多矿物蚀变综合评分图（高置信像元 {comp.get('high_confidence_pixels','?')}）",
                "source": "geo-analyser",
            })

    return {
        "aoi_name": md.get("project_name") or run_dir.parent.name,
        "deposit_type": md.get("deposit_type", ""),
        "run_dir": str(run_dir),
        "bbox": bbox,
        "created_at": md.get("created_at", ""),
        "results": results,
        "structural": md.get("structural", {}),
        "figures": figures,
        "trace_id": md.get("trace_id"),
        "linked_trace_ids": md.get("linked_trace_ids", []),
        "tenant_id": md.get("tenant_id"),
    }


def scan_alteration_outputs(geo_analyser_outputs: str = DEFAULT_GEO_ANALYSER_OUTPUTS) -> List[Dict]:
    """
    扫描 geo-analyser 输出，按 latest.json 取每个 AOI 各 deposit_type 的最新 run。
    无 latest.json 时回退扫描目录下所有含 manifest.json 的 run。
    """
    root = Path(geo_analyser_outputs)
    if not root.exists():
        return []

    out: List[Dict] = []
    for aoi_dir in root.iterdir():
        if not aoi_dir.is_dir():
            continue
        latest = _read_json(aoi_dir / "latest.json")
        run_rels: List[str] = []
        if latest:
            run_rels = list(latest.values())  # 形如 "<AOI>/<deposit>_<ts>"
        else:
            run_rels = [str(d.relative_to(root)) for d in aoi_dir.iterdir()
                        if d.is_dir() and (d / "manifest.json").exists()]
        for rel in run_rels:
            run_dir = root / rel
            md = _read_json(run_dir / "manifest.json")
            if md is None:
                continue
            entry = _entry_from_manifest(run_dir, md)
            if entry is not None:
                out.append(entry)
    return out


def find_alteration_for_bbox(
    bbox: Tuple[float, float, float, float],
    geo_analyser_outputs: str = DEFAULT_GEO_ANALYSER_OUTPUTS,
    trace_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> List[Dict]:
    """返回与给定 bbox 相交的蚀变分析成果，按 created_at 降序。

    trace_id（可选）：优先按 trace_id 精确匹配，未命中回退 bbox（见架构蓝图 §1.3）。
    """
    matches = [e for e in scan_alteration_outputs(geo_analyser_outputs)
               if _bbox_intersects(e.get("bbox"), bbox)]
    matches.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    try:
        from commons.trace import filter_by_trace_id, filter_by_tenant
        return filter_by_trace_id(filter_by_tenant(matches, tenant_id), trace_id)
    except Exception:
        return matches
