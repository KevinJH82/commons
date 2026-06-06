"""
auth.py — 跨子系统凭证管理

读取 credentials.yaml,优先用本子系统配置,fallback 到 geo-downloader 的(同机共享)。

结构(对齐 geo-downloader/config/credentials.yaml):
  nasa_earthdata:
    username: ...
    password: ...
    token: ...   # 可选,优先于密码认证(HyP3 推荐)
  copernicus: {...}
  usgs: {...}
"""

import os
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


class CredentialsError(Exception):
    pass


# Fallback 路径:同机部署时复用 geo-downloader 凭证
_GEO_DOWNLOADER_FALLBACK = Path("/opt/deepexplor-services/geo-downloader/config/credentials.yaml")


def load_credentials(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    加载 credentials.yaml。

    优先级:
    1. 命令行/参数 config_path
    2. 环境变量 GEO_INSAR_CONFIG
    3. 当前子系统 config/credentials.yaml(geo-insar 自带)
    4. fallback: /opt/deepexplor-services/geo-downloader/config/credentials.yaml
    """
    if not HAS_YAML:
        raise CredentialsError("缺少依赖: pyyaml — pip install pyyaml")

    candidates = []
    if config_path:
        candidates.append(Path(config_path))
    if os.environ.get("GEO_INSAR_CONFIG"):
        candidates.append(Path(os.environ["GEO_INSAR_CONFIG"]))
    # 当前子系统(caller cwd 上溯到 config/)
    candidates.append(Path.cwd() / "config" / "credentials.yaml")
    # geo-insar 自身的固定路径
    candidates.append(Path("/opt/deepexplor-services/geo-insar/config/credentials.yaml"))
    # geo-downloader fallback
    candidates.append(_GEO_DOWNLOADER_FALLBACK)

    for p in candidates:
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                creds = yaml.safe_load(f) or {}
            creds["_loaded_from"] = str(p)
            return creds

    raise CredentialsError(
        "找不到 credentials.yaml,候选路径:\n  " +
        "\n  ".join(str(c) for c in candidates) +
        "\n请复制 credentials.yaml.example 为对应位置并填写凭证。"
    )


def get_earthdata_creds(creds: Dict[str, Any]) -> Dict[str, str]:
    """
    提取 NASA Earthdata 凭证(HyP3 / ASF 共用)。

    Returns
    -------
    {"username": ..., "password": ..., "token": ...}
    """
    section = creds.get("nasa_earthdata")
    if not section:
        raise CredentialsError(
            "credentials.yaml 中缺少 nasa_earthdata 段。\n"
            "注册: https://urs.earthdata.nasa.gov/\n"
            "HyP3 授权: https://hyp3-api.asf.alaska.edu/ui/"
        )
    out = {
        "username": section.get("username", ""),
        "password": section.get("password", ""),
        "token": section.get("token", "") or "",
    }
    if not (out["username"] and (out["password"] or out["token"])):
        raise CredentialsError(
            "nasa_earthdata 凭证不完整:需要 username + (password 或 token)。"
        )
    return out
