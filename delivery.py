"""统一交付库定位(几何寻址 + 自描述清单)。

背景:交付目录(geo-downloader 产出,名=ROI名+下载任务编号)此前靠"目录名字符串匹配"
定位,KML 改名就断。本模块把定位改为:① 稳定 delivery_id → ② 名字(精确/归一) → ③ 几何覆盖,
并让每个交付写一份自描述 `delivery.json`(含 delivery_id/bbox/geojson/清单),"命名一次,
之后按 ID 引用"。

自包含:轻量 KML/geojson→bbox 解析(无 shapely/rasterio 依赖),任意服务可直接 import。
"""
from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MANIFEST_NAME = "delivery.json"
SCHEMA_VERSION = "1.0"
_INDEX_FILE = ".delivery_index.json"
_ROI_SUFFIXES = (".kml", ".ovkml", ".geojson", ".json")
_COVER_THRESHOLD = float(os.environ.get("DELIVERY_COVER_THRESHOLD", "0.6"))
WINTER_SUBDIR = "data-矿权-冬季（11-3月）"
SUMMER_SUBDIR = "data-矿权-夏季（6-8月）"


def delivery_root() -> Path:
    return Path(os.environ.get(
        "DELIVERY_ROOT", "/Volumes/大硬盘可劲用/DeepExplor/数据留存/交付数据"))


# ─── 轻量 ROI 解析(无重依赖) ───────────────────────────────
def _kml_ring(text: str) -> List[List[float]]:
    pts = []
    for tok in (text or "").replace("\n", " ").split():
        parts = tok.split(",")
        if len(parts) >= 2:
            try:
                pts.append([float(parts[0]), float(parts[1])])
            except ValueError:
                continue
    return pts


def parse_roi(path: Path) -> Optional[Dict[str, Any]]:
    """解析 ROI 文件 → GeoJSON Polygon。支持 .kml/.ovkml/.geojson/.json(无 shapely)。"""
    p = Path(path)
    suf = p.suffix.lower()
    try:
        if suf in (".kml", ".ovkml"):
            text = p.read_text(encoding="utf-8")
            if text.startswith("﻿"):
                text = text[1:]
            root = ET.fromstring(text)
            ns = {"kml": "http://www.opengis.net/kml/2.2"}
            nodes = root.findall(".//kml:Polygon//kml:coordinates", ns) or \
                root.findall(".//Polygon//coordinates")
            if not nodes:
                return None
            ring = _kml_ring(nodes[0].text or "")
            if len(ring) < 3:
                return None
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            return {"type": "Polygon", "coordinates": [ring]}
        if suf in (".geojson", ".json"):
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("type") == "FeatureCollection":
                d = (d.get("features") or [{}])[0].get("geometry")
            elif d.get("type") == "Feature":
                d = d.get("geometry")
            return d if d and d.get("type") in ("Polygon", "MultiPolygon") else None
    except Exception:
        return None
    return None


def bbox_of(geom: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    if not geom:
        return None
    pts: List[List[float]] = []
    if geom.get("type") == "Polygon":
        for ring in geom.get("coordinates") or []:
            pts.extend(ring)
    elif geom.get("type") == "MultiPolygon":
        for poly in geom.get("coordinates") or []:
            for ring in poly:
                pts.extend(ring)
    if not pts:
        return None
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return min(lons), min(lats), max(lons), max(lats)


# ─── 几何打分 ──────────────────────────────────────────────
def _inter(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy


def coverage(query, cand) -> float:
    """query ROI 被候选交付覆盖的比例(交集/query)。"""
    if not query or not cand:
        return 0.0
    qa = (query[2] - query[0]) * (query[3] - query[1])
    return _inter(query, cand) / qa if qa > 0 else 0.0


def iou(query, cand) -> float:
    if not query or not cand:
        return 0.0
    i = _inter(query, cand)
    qa = (query[2] - query[0]) * (query[3] - query[1])
    ca = (cand[2] - cand[0]) * (cand[3] - cand[1])
    u = qa + ca - i
    return i / u if u > 0 else 0.0


# ─── 命名 ──────────────────────────────────────────────────
def _task_id_of(dir_name: str) -> str:
    """从目录名尾部抽下载任务编号(6+位数字),作为 delivery_id 基。"""
    m = re.search(r"_(\d{6,})$", dir_name)
    return m.group(1) if m else ""


def delivery_id_for(dir_name: str) -> str:
    tid = _task_id_of(dir_name)
    return f"del_{tid}" if tid else f"del_{abs(hash(dir_name)) % (10**10):010d}"


def norm_name(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"_\d{6,}$", "", s)
    s = re.sub(r"\d+(\.\d+)?\s*km2", "", s)
    s = re.sub(r"[\s_\-（）()]+", "", s)
    return s


# ─── 清单 ──────────────────────────────────────────────────
def _roi_file(d: Path) -> Optional[Path]:
    for suf in _ROI_SUFFIXES:
        c = d / f"{d.name}{suf}"
        if c.is_file():
            return c
    try:
        for f in sorted(d.iterdir()):
            if f.is_file() and f.suffix.lower() in _ROI_SUFFIXES:
                return f
    except Exception:
        return None
    return None


def _inventory(d: Path) -> Dict[str, Any]:
    inv: Dict[str, Any] = {"winter": False, "summer": False, "sensors": []}
    try:
        for sub in d.iterdir():
            if sub.name == WINTER_SUBDIR:
                inv["winter"] = True
            elif sub.name == SUMMER_SUBDIR:
                inv["summer"] = True
        win = d / WINTER_SUBDIR
        if win.is_dir():
            inv["sensors"] = sorted(s.name for s in win.iterdir() if s.is_dir())
    except Exception:
        pass
    return inv


def build_manifest(delivery_dir: Path, *, bbox=None, roi_geojson=None,
                   roi_name: str = "", download_task_id: str = "",
                   extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """生成交付清单。bbox/geojson 缺省时从目录内 ROI 文件解析。"""
    d = Path(delivery_dir)
    roi_file = _roi_file(d)
    if roi_geojson is None and roi_file is not None:
        roi_geojson = parse_roi(roi_file)
    if bbox is None and roi_geojson is not None:
        bb = bbox_of(roi_geojson)
        bbox = list(bb) if bb else None
    man = {
        "schema_version": SCHEMA_VERSION,
        "delivery_id": delivery_id_for(d.name),
        "dir_name": d.name,
        "roi_name": roi_name or d.name,
        "download_task_id": download_task_id or _task_id_of(d.name),
        "bbox": list(bbox) if bbox else None,
        "roi_geojson": roi_geojson,
        "source_roi_file": roi_file.name if roi_file else None,
        "inventory": _inventory(d),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if extra:
        man.update(extra)
    return man


def read_manifest(delivery_dir: Path) -> Optional[Dict[str, Any]]:
    f = Path(delivery_dir) / MANIFEST_NAME
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_manifest(delivery_dir: Path, *, overwrite: bool = True, **kw) -> Optional[Dict[str, Any]]:
    """写 delivery.json(原子)。overwrite=False 时已存在则跳过。返回清单 dict。"""
    d = Path(delivery_dir)
    f = d / MANIFEST_NAME
    if f.exists() and not overwrite:
        return read_manifest(d)
    man = build_manifest(d, **kw)
    try:
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)
    except Exception:
        return man  # 只读盘等:返回清单但未落盘
    return man


def backfill(root: Optional[Path] = None, overwrite: bool = False) -> Dict[str, int]:
    """为交付根下所有目录补写 delivery.json(已有默认跳过)。"""
    root = Path(root) if root else delivery_root()
    stats = {"total": 0, "written": 0, "skipped": 0, "no_geom": 0}
    if not root.is_dir():
        return stats
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        stats["total"] += 1
        if (d / MANIFEST_NAME).exists() and not overwrite:
            stats["skipped"] += 1
            continue
        man = write_manifest(d, overwrite=overwrite)
        if man and man.get("bbox"):
            stats["written"] += 1
        else:
            stats["no_geom"] += 1
    return stats


# ─── 索引 + 解析 ───────────────────────────────────────────
def _index_key(root: Path) -> str:
    dirs = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")]
    mt = max((d.stat().st_mtime for d in dirs), default=0)
    return f"{len(dirs)}:{int(mt)}"


def build_index(root: Optional[Path] = None, force: bool = False) -> Dict[str, Dict[str, Any]]:
    """{dir_name: {delivery_id, bbox}} 索引,优先读 delivery.json,缺则解析内含 ROI。缓存。"""
    root = Path(root) if root else delivery_root()
    if not root.is_dir():
        return {}
    cache = root / _INDEX_FILE
    key = _index_key(root)
    if not force and cache.exists():
        try:
            c = json.loads(cache.read_text(encoding="utf-8"))
            if c.get("key") == key and "entries" in c:   # 校验格式,旧版 {bboxes} 视作失效重建
                return c["entries"]
        except Exception:
            pass
    entries: Dict[str, Dict[str, Any]] = {}
    for d in root.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        man = read_manifest(d)
        if man and man.get("bbox"):
            entries[d.name] = {"delivery_id": man.get("delivery_id"), "bbox": man["bbox"]}
        else:
            roi = _roi_file(d)
            bb = bbox_of(parse_roi(roi)) if roi else None
            if bb:
                entries[d.name] = {"delivery_id": delivery_id_for(d.name), "bbox": list(bb)}
    try:
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": key, "entries": entries}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache)
    except Exception:
        pass
    return entries


def resolve(name: str = "", roi_geojson: Optional[Dict[str, Any]] = None,
            delivery_id: str = "", root: Optional[Path] = None) -> Dict[str, Any]:
    """统一定位 → {dir, delivery_id, method, candidates}。

    解析链:delivery_id(精确) → 名字(精确/归一) → 几何覆盖。method 之一:
    id / exact / normalized / spatial / none。
    """
    root = Path(root) if root else delivery_root()
    if not root.is_dir():
        return {"dir": None, "delivery_id": None, "method": "none", "candidates": []}

    index = build_index(root)

    # ① delivery_id 精确
    if delivery_id:
        for dn, e in index.items():
            if e.get("delivery_id") == delivery_id:
                return {"dir": root / dn, "delivery_id": delivery_id, "method": "id", "candidates": []}

    main = Path(name).stem if name else ""
    # ② 精确名
    if main and (root / main).is_dir():
        return {"dir": root / main, "delivery_id": delivery_id_for(main),
                "method": "exact", "candidates": []}
    # ③ 归一名
    if main:
        target = norm_name(main)
        if target:
            for dn in sorted(index.keys()):
                if norm_name(dn) == target:
                    return {"dir": root / dn, "delivery_id": index[dn].get("delivery_id"),
                            "method": "normalized", "candidates": []}

    # ④ 几何覆盖
    qbbox = bbox_of(roi_geojson) if roi_geojson else None
    candidates: List[Dict[str, Any]] = []
    if qbbox:
        scored = []
        for dn, e in index.items():
            cov = coverage(qbbox, e["bbox"])
            if cov > 0:
                try:
                    mt = (root / dn).stat().st_mtime
                except Exception:
                    mt = 0
                scored.append({"name": dn, "delivery_id": e.get("delivery_id"),
                               "bbox": e["bbox"], "coverage": cov,
                               "iou": iou(qbbox, e["bbox"]), "mtime": mt})
        scored.sort(key=lambda x: (x["coverage"], x["iou"], x["mtime"]), reverse=True)
        candidates = [{"name": s["name"], "delivery_id": s["delivery_id"], "bbox": s["bbox"],
                       "coverage": round(s["coverage"], 3), "iou": round(s["iou"], 3)}
                      for s in scored[:5]]
        qualified = [s for s in scored if s["coverage"] >= _COVER_THRESHOLD]
        if qualified:
            best = max(qualified, key=lambda x: (x["iou"], x["mtime"]))
            return {"dir": root / best["name"], "delivery_id": best.get("delivery_id"),
                    "method": "spatial", "candidates": candidates}

    return {"dir": None, "delivery_id": None, "method": "none", "candidates": candidates}
