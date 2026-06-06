"""
insar_utils.py — InSAR 公共工具函数

所有子系统(geo-insar / geo-exploration / geo-reporter / geo-analyser)
共享的 InSAR 处理工具:LOS 分解、相干性掩膜、堆栈管理、metadata 验证。
"""

import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

_SCHEMA_PATH = Path(__file__).parent / "insar_schema.json"


def los_to_vertical(disp_los: np.ndarray, incidence_angle_deg: float) -> np.ndarray:
    """
    将 LOS(视线方向)形变分量转换为垂直分量(假设纯垂直运动)。

    disp_vert = disp_los / cos(incidence_angle)

    Parameters
    ----------
    disp_los : np.ndarray
        LOS 方向形变(mm 或 m,单位透传)
    incidence_angle_deg : float
        入射角(度),从 metadata.incidence_angle_mean 读取

    Returns
    -------
    np.ndarray
        假设纯垂直运动时的垂直形变,与输入同单位
    """
    cos_inc = math.cos(math.radians(incidence_angle_deg))
    if abs(cos_inc) < 1e-6:
        raise ValueError(f"入射角接近 90°,LOS 分量无法转换: {incidence_angle_deg}")
    return disp_los / cos_inc


def coherence_mask(
    coherence: np.ndarray,
    threshold: float = 0.3,
    nodata_value: float = np.nan,
) -> np.ndarray:
    """
    根据相干性阈值生成布尔掩膜。

    Parameters
    ----------
    coherence : np.ndarray, 0–1
    threshold : float, 默认 0.3
    nodata_value : 相干性为 nodata 时的填充值(NaN)

    Returns
    -------
    np.ndarray of bool, True 表示可靠像素
    """
    mask = coherence >= threshold
    if not np.isnan(nodata_value):
        mask &= coherence != nodata_value
    else:
        mask &= ~np.isnan(coherence)
    return mask


def apply_coherence_mask(
    disp: np.ndarray,
    coherence: np.ndarray,
    threshold: float = 0.3,
) -> np.ndarray:
    """形变图按相干性阈值掩膜,不可靠像素设为 NaN。"""
    mask = coherence_mask(coherence, threshold=threshold)
    out = disp.astype(np.float32, copy=True)
    out[~mask] = np.nan
    return out


def read_pair_metadata(pair_dir: Path) -> Dict:
    """读取 InSAR 对目录下的 metadata.json。"""
    p = Path(pair_dir) / "metadata.json"
    if not p.exists():
        raise FileNotFoundError(f"metadata.json 不存在: {p}")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_metadata(meta: Dict) -> Tuple[bool, List[str]]:
    """
    用 insar_schema.json 校验 metadata。

    Returns
    -------
    (ok, errors)
    """
    try:
        import jsonschema
    except ImportError:
        # jsonschema 不强制依赖,缺失时只做最基本字段检查
        errors = []
        required = ["pair_id", "master_date", "slave_date", "source", "products"]
        for k in required:
            if k not in meta:
                errors.append(f"缺少字段: {k}")
        return (len(errors) == 0, errors)

    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = json.load(f)
    validator = jsonschema.Draft7Validator(schema)
    errors = [e.message for e in validator.iter_errors(meta)]
    return (len(errors) == 0, errors)


def find_pairs(aoi_output_dir: Path) -> List[Path]:
    """
    扫描 AOI 输出目录,返回所有合法 InSAR 对目录(含 metadata.json 的)。

    目录结构:
      {aoi_output_dir}/sentinel1_insar/<refdate>_<secdate>_<pol>/metadata.json
    """
    aoi_output_dir = Path(aoi_output_dir)
    pairs = []
    if not aoi_output_dir.exists():
        return pairs
    for sensor_dir in aoi_output_dir.iterdir():
        if not sensor_dir.is_dir():
            continue
        for pair_dir in sensor_dir.iterdir():
            if pair_dir.is_dir() and (pair_dir / "metadata.json").exists():
                pairs.append(pair_dir)
    return sorted(pairs)


def stack_summary(aoi_output_dir: Path) -> Dict:
    """
    汇总 AOI 下所有干涉对的统计信息,用于 geo-reporter / geo-exploration 消费。

    Returns
    -------
    dict: {
        "pair_count": N,
        "date_range": [earliest, latest],
        "temporal_baselines_days": [...],
        "polarizations": [...],
        "orbit_directions": [...],
        "coherence_mean_overall": ...,
        "pairs": [pair_meta, ...]
    }
    """
    pairs = find_pairs(aoi_output_dir)
    metas = [read_pair_metadata(p) for p in pairs]
    if not metas:
        return {"pair_count": 0, "pairs": []}

    dates = sorted(set([m["master_date"] for m in metas] + [m["slave_date"] for m in metas]))
    coh_means = [m.get("stats", {}).get("coherence_mean") for m in metas if m.get("stats", {}).get("coherence_mean") is not None]

    return {
        "pair_count": len(metas),
        "date_range": [dates[0], dates[-1]] if dates else [None, None],
        "temporal_baselines_days": [m["temporal_baseline_days"] for m in metas],
        "polarizations": sorted(set(m["polarization"] for m in metas)),
        "orbit_directions": sorted(set(m["orbit_direction"] for m in metas)),
        "coherence_mean_overall": float(np.mean(coh_means)) if coh_means else None,
        "pairs": metas,
    }


def compute_pair_stats(pair_dir: Path) -> Dict:
    """
    读取一个干涉对目录下的 GeoTIFF,计算快速统计。
    用于在标准化输出时把 stats 写入 metadata.json。
    """
    try:
        import rasterio
    except ImportError:
        return {}

    stats = {
        "coherence_mean": None,
        "coherence_median": None,
        "los_displacement_min_mm": None,
        "los_displacement_max_mm": None,
        "los_displacement_mean_mm": None,
        "coverage_ratio": None,
    }

    coh_tif = Path(pair_dir) / "coherence.tif"
    if coh_tif.exists():
        try:
            with rasterio.open(coh_tif) as src:
                arr = src.read(1, masked=True)
                stats["coherence_mean"] = float(np.ma.mean(arr))
                stats["coherence_median"] = float(np.ma.median(arr))
                total = arr.size
                valid = total - int(np.ma.count_masked(arr))
                stats["coverage_ratio"] = float(valid / total) if total > 0 else None
        except Exception:
            pass

    disp_tif = Path(pair_dir) / "los_displacement.tif"
    if disp_tif.exists():
        try:
            with rasterio.open(disp_tif) as src:
                arr = src.read(1, masked=True)
                stats["los_displacement_min_mm"] = float(np.ma.min(arr))
                stats["los_displacement_max_mm"] = float(np.ma.max(arr))
                stats["los_displacement_mean_mm"] = float(np.ma.mean(arr))
        except Exception:
            pass

    return stats
