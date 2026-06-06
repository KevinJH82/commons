"""
aoi.py — AOI(感兴趣区)解析

委托给 geo-downloader/downloader/kml_parser 的成熟实现(已支持
.kml / .ovkml / .kmz / .ovkmz / .xlsx / .xls,以及多命名空间)。

关键设计:**不修改 sys.path**,用 importlib.util 从绝对路径直接加载
kml_parser.py。原因:geo-downloader 和 geo-insar 都有顶级 `downloader/`
和 `postprocess/` 包,如果把 geo-downloader 塞进 sys.path 会污染调用方
(insert(0) 遮蔽 geo-insar 的 postprocess;append 遮蔽 geo-downloader
自己的 downloader 子包定位),所以采用零污染的 importlib 方案。
"""

import importlib.util
import sys
from pathlib import Path
from typing import Tuple, Optional, Any

_GEO_DOWNLOADER_PATH = Path("/opt/deepexplor-services/geo-downloader")
_KML_PARSER_PATH = _GEO_DOWNLOADER_PATH / "downloader" / "kml_parser.py"


_kml_parser_module = None  # 懒加载缓存


def _load_kml_parser():
    """从绝对路径加载 geo-downloader 的 kml_parser 模块,不污染 sys.path。"""
    global _kml_parser_module
    if _kml_parser_module is not None:
        return _kml_parser_module

    if not _KML_PARSER_PATH.exists():
        raise ImportError(
            f"找不到 {_KML_PARSER_PATH}\n"
            "geo-downloader 似乎不存在,无法复用其 KML 解析器。"
        )

    # 用一个独特的模块名,避免与任何子系统的 'kml_parser' 冲突
    spec = importlib.util.spec_from_file_location(
        "geo_downloader_kml_parser",  # 独立命名空间
        str(_KML_PARSER_PATH),
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法创建 spec: {_KML_PARSER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _kml_parser_module = module
    return module


def parse_aoi(kml_path: str, point_buffer_deg: float = 0.1):
    """
    解析 KML/OVKML/KMZ/Excel,返回 (bbox, geometry, area_name)。

    Parameters
    ----------
    kml_path : str
        文件路径,扩展名可为 .kml / .ovkml / .kmz / .ovkmz / .xlsx / .xls
    point_buffer_deg : float
        点要素缓冲半径(度),默认 0.1°≈11km

    Returns
    -------
    bbox : (min_lon, min_lat, max_lon, max_lat)
    geometry : shapely 几何对象(可用于裁剪)
    area_name : str(自动从文件名提取)
    """
    try:
        kml_parser = _load_kml_parser()
    except ImportError as e:
        raise ImportError(
            f"无法加载 KML 解析器: {e}\n"
            "请确认 /opt/deepexplor-services/geo-downloader/downloader/kml_parser.py 存在,"
            "且已 pip install lxml shapely。"
        )

    path = Path(kml_path)
    if not path.exists():
        raise FileNotFoundError(f"KML 文件不存在: {kml_path}")

    # geo-downloader 的 parse_kml 返回 (geometry, bbox, name)
    geometry, bbox, name = kml_parser.parse_kml(str(path), point_buffer_deg=point_buffer_deg)

    # name 来自 KML 内的 <name>;如果空则回退到文件名
    area_name = name or path.stem

    return bbox, geometry, area_name


def bbox_to_wkt(bbox: Tuple[float, float, float, float]) -> str:
    """bbox → WKT POLYGON(asf_search 需要的格式)。"""
    min_lon, min_lat, max_lon, max_lat = bbox
    return (
        f"POLYGON(({min_lon} {min_lat},{max_lon} {min_lat},"
        f"{max_lon} {max_lat},{min_lon} {max_lat},{min_lon} {min_lat}))"
    )


def bbox_area_km2(bbox: Tuple[float, float, float, float]) -> float:
    """估算 bbox 面积(km²),用经纬度近似(适合 < 100 km 的 AOI)。"""
    import math
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = (min_lat + max_lat) / 2.0
    lon_km = (max_lon - min_lon) * 111.32 * math.cos(math.radians(mid_lat))
    lat_km = (max_lat - min_lat) * 110.57
    return abs(lon_km * lat_km)
