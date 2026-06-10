"""geochem_public_broker.py — 发现并加载「预置到本地的公开地球化学数据集」。

注册表 / 预置式、数据源无关：broker **不联网取数**，只发现已落地到
PUBLIC_GEOCHEM_ROOT 的公开化探点位数据集，按 AOI(bbox) 相交命中。
- 海外开放集（USGS NGDB / 澳洲 NGSA / 欧洲 FOREGS·GEMAS 等）可脚本化下载入库；
- 中国 RGNR/CGB 等点位级数据不开放，需用户合法获取后按格式放入注册表即自动生效。

目录布局：
    <PUBLIC_GEOCHEM_ROOT>/index.json          # 数据集注册表
    <PUBLIC_GEOCHEM_ROOT>/<dataset files...>   # CSV / GeoJSON 点位文件

注册表 index.json 形如：
    {"datasets": [
       {"name": "ngsa_demo", "source": "Geoscience Australia NGSA",
        "license": "CC BY 4.0", "bbox": [w, s, e, n], "crs": "EPSG:4326",
        "path": "ngsa_demo.csv", "scale_note": "区域尺度水系沉积物，约 1 点/数百 km²",
        "columns": {"lon": "LONGITUDE", "lat": "LATITUDE",
                    "elements": {"Cu": "Cu_ppm", "Pb": "Pb_ppm"}}}     # columns 可选
    ]}
（也兼容顶层直接是数组的写法。）

只读、高失败容忍：任何异常 → 跳过该数据集，绝不向上抛断主流程。
"""

import json
import os
from typing import Dict, List, Optional, Tuple

import pandas as pd


def _index_path(root: str) -> str:
    return os.path.join(root, "index.json")


def _load_index(root: str) -> List[Dict]:
    """读注册表，返回数据集条目列表（容错：缺失/坏 JSON → []）。"""
    ip = _index_path(root)
    if not root or not os.path.isfile(ip):
        return []
    try:
        with open(ip, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    items = data.get("datasets", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [d for d in items if isinstance(d, dict) and d.get("path")]


def _bbox_intersects(a, b) -> bool:
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def find_public_geochem_for_bbox(bbox, root: str) -> List[Dict]:
    """返回与 bbox 相交、且数据文件存在的公开数据集条目（已附绝对路径 _abspath）。"""
    out: List[Dict] = []
    for e in _load_index(root):
        if not _bbox_intersects(e.get("bbox"), bbox):
            continue
        p = e.get("path")
        ap = p if os.path.isabs(p) else os.path.join(root, p)
        if os.path.isfile(ap):
            e2 = dict(e)
            e2["_abspath"] = ap
            out.append(e2)
    return out


def _geojson_to_df(path: str) -> Optional[pd.DataFrame]:
    with open(path, "r", encoding="utf-8") as f:
        fc = json.load(f)
    rows = []
    for ft in fc.get("features", []):
        geom = ft.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        row = {"lon": coords[0], "lat": coords[1]}
        row.update(ft.get("properties") or {})
        rows.append(row)
    return pd.DataFrame(rows) if rows else None


def load_public_geochem_df(entry: Dict) -> Tuple[Optional[pd.DataFrame], Optional[Dict]]:
    """读条目数据文件 → (DataFrame, columns_override)。失败返回 (None, None)。

    支持 .csv/.txt 与 .geojson/.json（点要素 → lon,lat + properties 列）。
    columns_override 来自注册表条目的可选 "columns" 字段，交由调用方（ingest）按其
    既有列识别逻辑应用，保持列检测单一来源。
    """
    ap = entry.get("_abspath") or entry.get("path")
    if not ap or not os.path.isfile(ap):
        return None, None
    ext = ap.rsplit(".", 1)[-1].lower() if "." in ap else ""
    try:
        if ext in ("csv", "txt"):
            df = pd.read_csv(ap)
        elif ext in ("geojson", "json"):
            df = _geojson_to_df(ap)
        else:
            return None, None
    except Exception:
        return None, None
    if df is None or df.empty:
        return None, None
    return df, entry.get("columns")
