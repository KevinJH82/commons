"""
commons/spectral_indices.py — 跨系统共享的纯光谱指数函数

这些函数无状态、无 I/O,被多个子系统复用:
  - geo-preprocess(数据预处理): 用 NDVI/NDWI/MNDWI/NDBI/NDSI 生成干扰掩膜
  - geo-analyser(蚀变分析): 用 NDVI + 红边指数(NDRE/CIre/REP)做丛林模式地植物学胁迫;
                            用 BSI 做天然出露检测

抽到 commons 后,geo-analyser 不再依赖 geo-preprocess 的 interference_removal,二者经此共享。
"""

import numpy as np


def _safe_divide(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """避免除零，返回 float32"""
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(b != 0, a.astype(np.float32) / b.astype(np.float32), 0.0)
    return result.astype(np.float32)


# ─────────────────────────────────────────────
# 常规归一化指数
# ─────────────────────────────────────────────

def calc_ndvi(nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    """
    归一化植被指数 NDVI = (NIR - Red) / (NIR + Red)
    范围 [-1, 1]，植被通常 > 0.2
    """
    return _safe_divide(nir - red, nir + red)


def calc_ndwi(green: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """
    归一化水体指数 NDWI = (Green - NIR) / (Green + NIR)
    范围 [-1, 1]，水体通常 > 0.0
    """
    return _safe_divide(green - nir, green + nir)


def calc_mndwi(green: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    """
    改进归一化水体指数 MNDWI = (Green - SWIR1) / (Green + SWIR1)
    对混浊水体效果优于 NDWI
    """
    return _safe_divide(green - swir1, green + swir1)


def calc_ndbi(swir1: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """
    归一化建筑指数 NDBI = (SWIR1 - NIR) / (SWIR1 + NIR)
    建筑/裸地通常 > 0.0
    """
    return _safe_divide(swir1 - nir, swir1 + nir)


def calc_bsi(blue: np.ndarray, red: np.ndarray,
             nir: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    """
    裸土指数 BSI = ((SWIR1 + Red) - (NIR + Blue)) / ((SWIR1 + Red) + (NIR + Blue))
    用于区分裸露岩石/土壤与植被
    """
    numerator   = (swir1 + red) - (nir + blue)
    denominator = (swir1 + red) + (nir + blue)
    return _safe_divide(numerator, denominator)


# ─────────────────────────────────────────────
# 红边 / 地植物学胁迫指数（丛林模式）
# 密林区岩石被冠层遮蔽,无法直接做蚀变;转而探测"矿化/微渗漏导致的植物胁迫"——
# 叶绿素↓ → 红边蓝移、红边反射↓。这些指数在"有植被"像元上计算长势,
# 低值=受胁迫=潜在矿化指示。需含红边波段的传感器(Sentinel-2 / EnMAP / PRISMA)。
# ─────────────────────────────────────────────

def calc_ndre(nir: np.ndarray, rededge1: np.ndarray) -> np.ndarray:
    """
    红边归一化指数 NDRE = (NIR - RE1) / (NIR + RE1)
    （Sentinel-2: RE1=B5）。健康植被高,叶绿素受胁迫时显著降低。
    """
    return _safe_divide(nir - rededge1, nir + rededge1)


def calc_cire(rededge3: np.ndarray, rededge1: np.ndarray) -> np.ndarray:
    """
    红边叶绿素指数 CIre = (RE3 / RE1) - 1（Sentinel-2: RE3=B7, RE1=B5）。
    与叶绿素含量正相关;胁迫→值↓。
    """
    return _safe_divide(rededge3, rededge1) - 1.0


def calc_rep(red: np.ndarray, rededge1: np.ndarray,
             rededge2: np.ndarray, rededge3: np.ndarray,
             wl: tuple = (665.0, 705.0, 740.0, 783.0)) -> np.ndarray:
    """
    红边位置 REP (nm) — Guyot & Baret 线性内插法(Sentinel-2 B4/B5/B6/B7 适配)。

    R_ideal = (Red + RE3) / 2
    REP = λ(RE1) + (λ(RE2) - λ(RE1)) * (R_ideal - RE1) / (RE2 - RE1)

    叶绿素受胁迫↓ → 红边向短波"蓝移"(REP 减小)。返回 nm,无效处为 NaN。
    """
    w_red, w1, w2, w3 = wl
    r_ideal = (red.astype(np.float32) + rededge3.astype(np.float32)) / 2.0
    frac = _safe_divide(r_ideal - rededge1, rededge2 - rededge1)
    rep = (w1 + (w2 - w1) * frac).astype(np.float32)
    # 内插分母≈0 处 _safe_divide 返回 0 → REP 退化为 λ(RE1),标记为 NaN 更诚实
    bad = ~np.isfinite(rep) | (np.abs((rededge2 - rededge1)) < 1e-9)
    rep[bad] = np.nan
    return rep
