"""
datacolle_broker.py — 订阅 data-colle（prospector）标准输出

与 structural_broker / insar_broker 同思路：纯文件系统订阅，零消息队列，高失败容忍，只读。
data-colle 产物布局：<output>/<AOI>_<矿种>_<时间戳>/
    ├── 00_项目摘要.md         （主报告文本，按章节标题分节）
    ├── summary.json           （结构化摘要，含 roi_bbox）
    ├── task_meta.json         （任务元数据，含 result.bbox / mineral，可能缺失）
    ├── viz_data.json          （化探元素阈值表 + 论文，可能缺失）
    ├── 02_地球物理资料/**.png （物探图件）
    └── 03_地球化学资料/

下游（geo-reporter）按 bbox 相交发现与本研究区匹配的 data-colle 成果，
把「地质 / 地球物理 / 地球化学」三部分文本与物探图件注入对应章节。
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_DATACOLLE_OUTPUTS = "/opt/deepexplor-services/data-colle/prospector/output"

# 物探图件文件名 → 中文图注
_FIGURE_CAPTIONS = {
    "emag2_upcont_map.png": "EMAG2 向上延拓磁异常分布图",
    "gravity_disturbance_map.png": "ICGEM 重力扰动图",
    "geoid_height_map.png": "大地水准面高程图",
}


def _bbox_intersects(a, b) -> bool:
    """两个 [min_lon,min_lat,max_lon,max_lat] 是否相交。"""
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _read_json(p: Path) -> Optional[dict]:
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _resolve_bbox(out_dir: Path) -> Optional[List[float]]:
    """从 task_meta.json（优先）或 summary.json 解析 [min_lon,min_lat,max_lon,max_lat]。"""
    tm = _read_json(out_dir / "task_meta.json")
    if tm:
        bb = (tm.get("result") or {}).get("bbox")
        if bb and all(k in bb for k in ("west", "south", "east", "north")):
            return [bb["west"], bb["south"], bb["east"], bb["north"]]
    sm = _read_json(out_dir / "summary.json")
    if sm:
        bb = (sm.get("metadata") or {}).get("roi_bbox")
        if bb and all(k in bb for k in ("west", "south", "east", "north")):
            return [bb["west"], bb["south"], bb["east"], bb["north"]]
    return None


# 地理与地形地貌相关关键词（用于从 ### 子节中识别地形内容）
_GEOGRAPHY_KW = ("地形", "地貌", "DEM", "高程", "海拔", "坡度", "数字高程")


def _split_markdown_sections(md_text: str) -> Dict[str, str]:
    """
    切分 markdown 并按关键词归类：
    - 二级标题（## ）整节 → geology/geophysics/geochemistry/remote_sensing
    - 三级标题（### ）中与地形地貌相关的子节（如「### DEM 地形数据」）→ geography
    返回 {category_id: 拼接后的文本}。
    """
    routed: Dict[str, List[str]] = {
        "geology": [], "geography": [], "geophysics": [],
        "geochemistry": [], "remote_sensing": []
    }
    # 一级路由：按 "## " 切分（保留标题行）
    for part in re.split(r"(?m)^(?=##\s)", md_text):
        head = part.lstrip().split("\n", 1)[0] if part.strip() else ""
        if not head.startswith("##"):
            continue
        body = part.strip()
        if "地球物理" in head:
            routed["geophysics"].append(body)
        elif "地球化学" in head or "化探" in head:
            routed["geochemistry"].append(body)
        elif "地质" in head:
            routed["geology"].append(body)
        elif "遥感" in head:
            routed["remote_sensing"].append(body)
    # 二级路由：抽取与地形地貌相关的 ### 子节 → geography（不影响一级归类，允许少量重复）
    for sub in re.split(r"(?m)^(?=###\s)", md_text):
        sub_head = sub.lstrip().split("\n", 1)[0] if sub.strip() else ""
        if sub_head.startswith("###") and any(k in sub_head for k in _GEOGRAPHY_KW):
            routed["geography"].append(sub.strip())
    return {k: "\n\n".join(v) for k, v in routed.items() if v}


def _collect_geophysics_figures(out_dir: Path) -> List[Dict]:
    """收集 02_地球物理资料/ 下的 PNG 图件（绝对路径 + 图注）。"""
    figs: List[Dict] = []
    geo_dir = out_dir / "02_地球物理资料"
    if not geo_dir.is_dir():
        return figs
    for png in sorted(geo_dir.rglob("*.png")):
        figs.append({
            "path": str(png),
            "caption": _FIGURE_CAPTIONS.get(png.name, png.stem),
            "source": "data-colle",
        })
    return figs


def scan_datacolle_outputs(datacolle_outputs: str = DEFAULT_DATACOLLE_OUTPUTS) -> List[Dict]:
    """
    扫描 data-colle 输出目录，返回每个 run 的摘要条目（含 bbox / 分节文本 / 物探图件）。

    Returns
    -------
    [{aoi_name, out_dir, bbox, mineral, created_at, sections{cat_id:text},
      figures[], geochem_thresholds}, ...]
    """
    root = Path(datacolle_outputs)
    if not root.exists():
        return []

    out: List[Dict] = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        bbox = _resolve_bbox(d)
        if bbox is None:
            continue
        md_path = d / "00_项目摘要.md"
        sections: Dict[str, str] = {}
        if md_path.exists():
            try:
                sections = _split_markdown_sections(md_path.read_text(encoding="utf-8"))
            except Exception:
                sections = {}
        tm = _read_json(d / "task_meta.json") or {}
        sm = _read_json(d / "summary.json") or {}
        viz = _read_json(d / "viz_data.json") or {}
        out.append({
            "aoi_name": tm.get("output_name") or d.name,
            "out_dir": str(d),
            "bbox": bbox,
            "mineral": tm.get("mineral") or (sm.get("metadata") or {}).get("mineral", ""),
            "created_at": tm.get("created_at") or (sm.get("metadata") or {}).get("generated_at", ""),
            "sections": sections,
            "figures": _collect_geophysics_figures(d),
            "geochem_thresholds": viz.get("thresholds", {}),
            "trace_id": tm.get("trace_id"),
            "linked_trace_ids": tm.get("linked_trace_ids", []),
            "tenant_id": tm.get("tenant_id"),
        })
    return out


def find_datacolle_for_bbox(
    bbox: Tuple[float, float, float, float],
    datacolle_outputs: str = DEFAULT_DATACOLLE_OUTPUTS,
    trace_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> List[Dict]:
    """返回与给定 bbox 相交的 data-colle 成果，按 created_at 降序（最新在前）。

    trace_id（可选）：优先按 trace_id 精确匹配，未命中回退 bbox（见架构蓝图 §1.3）。
    """
    matches = [e for e in scan_datacolle_outputs(datacolle_outputs)
               if _bbox_intersects(e.get("bbox"), bbox)]
    matches.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    try:
        from commons.trace import filter_by_trace_id, filter_by_tenant
        return filter_by_trace_id(filter_by_tenant(matches, tenant_id), trace_id)
    except Exception:
        return matches
