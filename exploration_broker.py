"""
exploration_broker.py — 订阅 geo-exploration 矿产深部探测标准输出

纯文件系统订阅，只读，高失败容忍。geo-exploration 产物布局：
    <uploads>/results_<task_id>/mineral_analysis_<时间戳>/
        ├── metadata.json          （机读契约，source=geo-exploration）
        ├── 03_深部成矿预测图.png 等  （可视化图件）
        ├── mineral_prediction.kmz
        └── <矿种>_Result.mat / *.npy

下游（geo-reporter）按 bbox 相交发现匹配本研究区的深部探测成果，注入「遥感影像」章节，
并据 prospecting_targets（top-20 靶区坐标）生成靶区推荐图。
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_GEO_EXPLORATION_OUTPUTS = (
    "/opt/deepexplor-services/geo-exploration/Python_Project/web_app/uploads"
)

_FIGURE_CAPTIONS = {
    "01_共振参数综合图.png": "共振参数综合图",
    "02_掩码集成.png": "异常掩码集成图",
    "03_深部成矿预测图.png": "深部成矿预测图",
    "04_深度反演图.png": "深度反演图",
    "05_压力反演图.png": "压力反演图",
}


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


def _collect_figures(run_dir: Path, products: dict) -> List[Dict]:
    figs: List[Dict] = []
    seen = set()
    # 优先按 products 声明的顺序
    for rel in (products or {}).values():
        if not isinstance(rel, str) or not rel.endswith(".png"):
            continue
        p = run_dir / rel
        if p.exists() and p.name not in seen:
            figs.append({"path": str(p),
                         "caption": _FIGURE_CAPTIONS.get(p.name, p.stem),
                         "source": "geo-exploration"})
            seen.add(p.name)
    # 兜底：扫描目录内未声明的 PNG
    for p in sorted(run_dir.glob("*.png")):
        if p.name not in seen:
            figs.append({"path": str(p),
                         "caption": _FIGURE_CAPTIONS.get(p.name, p.stem),
                         "source": "geo-exploration"})
            seen.add(p.name)
    return figs


def scan_exploration_outputs(
    geo_exploration_outputs: str = DEFAULT_GEO_EXPLORATION_OUTPUTS,
) -> List[Dict]:
    """
    扫描 geo-exploration 输出，返回每个 mineral_analysis run 的条目。

    Returns
    -------
    [{aoi_name, run_dir, bbox, mineral_type, created_at, statistics,
      prospecting_targets[], figures[]}, ...]
    """
    root = Path(geo_exploration_outputs)
    if not root.exists():
        return []

    out: List[Dict] = []
    # results_<task_id>/mineral_analysis_<ts>/metadata.json
    for meta_path in root.glob("results_*/mineral_analysis_*/metadata.json"):
        md = _read_json(meta_path)
        if not md or md.get("source") != "geo-exploration":
            continue
        bbox = md.get("aoi_bbox")
        if not bbox:
            continue
        run_dir = meta_path.parent
        out.append({
            "aoi_name": md.get("aoi_name") or md.get("task_name") or run_dir.parent.name,
            "run_dir": str(run_dir),
            "bbox": bbox,
            "mineral_type": md.get("mineral_type", ""),
            "created_at": md.get("created_at", ""),
            "statistics": md.get("statistics", {}),
            "prospecting_targets": md.get("prospecting_targets", []),
            "figures": _collect_figures(run_dir, md.get("products", {})),
            "trace_id": md.get("trace_id"),
            "linked_trace_ids": md.get("linked_trace_ids", []),
            "tenant_id": md.get("tenant_id"),
        })
    return out


def find_exploration_for_bbox(
    bbox: Tuple[float, float, float, float],
    geo_exploration_outputs: str = DEFAULT_GEO_EXPLORATION_OUTPUTS,
    trace_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> List[Dict]:
    """返回与给定 bbox 相交的深部探测成果，按 created_at 降序。

    trace_id（可选）：优先按 trace_id 精确匹配，未命中回退 bbox（见架构蓝图 §1.3）。
    """
    matches = [e for e in scan_exploration_outputs(geo_exploration_outputs)
               if _bbox_intersects(e.get("bbox"), bbox)]
    matches.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    try:
        from commons.trace import filter_by_trace_id, filter_by_tenant
        return filter_by_trace_id(filter_by_tenant(matches, tenant_id), trace_id)
    except Exception:
        return matches
