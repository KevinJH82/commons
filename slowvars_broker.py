"""
slowvars_broker.py — subscribe to geo-7slow standard outputs.

Expected layout:
    <results>/<run_id>/metadata.json
    <results>/<run_id>/delta_discriminant.tif
    <results>/<run_id>/target_zones.geojson

The broker is read-only and fault-tolerant, matching the existing commons broker
style used by model3d/reporter/drill.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GEO_SLOWVARS_OUTPUTS = os.environ.get(
    "GEO_SLOWVARS_OUTPUTS",
    str(_REPO_ROOT / "geo-7slow" / "backend" / "data" / "results"),
)


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _bbox_intersects(a, b) -> bool:
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _entry_from_metadata(run_dir: Path, md: dict) -> Optional[Dict]:
    if md.get("source") != "geo-7slow":
        return None
    bbox = md.get("aoi_bbox")
    products = md.get("products") or {}
    if not bbox or not products:
        return None
    return {
        "aoi_name": md.get("aoi_name") or run_dir.name,
        "aoi_bbox": bbox,
        "run_id": md.get("run_id") or run_dir.name,
        "slowvars_dir": str(run_dir),
        "metadata_path": str(run_dir / "metadata.json"),
        "created_at": md.get("created_at", ""),
        "products": products,
        "model_stats": md.get("model_stats", {}),
        "trace_id": md.get("trace_id"),
        "linked_trace_ids": md.get("linked_trace_ids", []),
        "tenant_id": md.get("tenant_id"),
    }


def scan_slowvars_outputs(
    geo_slowvars_outputs: str = DEFAULT_GEO_SLOWVARS_OUTPUTS,
) -> List[Dict]:
    """Scan geo-7slow result runs and return entries with standard metadata."""
    root = Path(geo_slowvars_outputs)
    if not root.exists():
        return []
    out: List[Dict] = []
    for run_dir in sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True):
        md = _read_json(run_dir / "metadata.json")
        if not md:
            continue
        entry = _entry_from_metadata(run_dir, md)
        if entry is not None:
            out.append(entry)
    return out


def find_slowvars_for_bbox(
    bbox: Tuple[float, float, float, float],
    geo_slowvars_outputs: str = DEFAULT_GEO_SLOWVARS_OUTPUTS,
    trace_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> List[Dict]:
    """Return slow variable runs intersecting bbox, preferring trace_id when supplied."""
    matches = [
        e for e in scan_slowvars_outputs(geo_slowvars_outputs)
        if _bbox_intersects(e.get("aoi_bbox"), bbox)
    ]
    matches.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    try:
        from commons.trace import filter_by_trace_id, filter_by_tenant
        return filter_by_trace_id(filter_by_tenant(matches, tenant_id), trace_id)
    except Exception:
        return matches


def get_product_path(entry: Dict, key: str) -> Optional[str]:
    """Resolve a product key to an absolute path if it exists."""
    rel = (entry.get("products") or {}).get(key)
    if not rel or not isinstance(rel, str):
        return None
    path = Path(entry["slowvars_dir"]) / rel
    return str(path) if path.exists() else None


def load_target_zones(entry: Dict) -> List[Dict]:
    """Load polygonized target-zone features for a slowvars run."""
    path = get_product_path(entry, "target_zones_geojson")
    if not path:
        return []
    payload = _read_json(Path(path)) or {}
    return payload.get("features", []) or []
