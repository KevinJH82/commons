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

import base64
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


class CredentialsError(Exception):
    pass


# ── NASA Earthdata token 自动换取 ──────────────────────────────
# Earthdata 已收紧程序化的「用户名+密码」登录（asf_search.auth_with_creds /
# hyp3_sdk 可能直接报 "Username or password is incorrect"）。官方推荐用 EDL token。
# 这里用 URS API 拿/建一个有效 token，调用方只需照常给 username/password 即可。
_URS = "https://urs.earthdata.nasa.gov"
_token_cache: Dict[str, str] = {}  # username -> access_token（进程内缓存）


def _jwt_exp(token: str) -> Optional[int]:
    """解出 JWT 的 exp（unix 秒）；不可解析返回 None。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # base64 补齐
        data = json.loads(base64.urlsafe_b64decode(payload).decode())
        return int(data["exp"]) if data.get("exp") else None
    except Exception:
        return None


def _token_expired(token: str, skew_sec: int = 300) -> bool:
    """token 是否已过期（含 5 分钟提前量）；不可解析则视为未过期、照常使用。"""
    exp = _jwt_exp(token)
    if exp is None:
        return False
    return exp <= time.time() + skew_sec


def _urs_request(method: str, path: str, username: str, password: str) -> Any:
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(
        _URS + path, method=method,
        headers={"Authorization": "Basic " + auth},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode()
    return json.loads(body) if body else None


def fetch_earthdata_token(username: str, password: str) -> Optional[str]:
    """
    用 URS API 取一个有效 EDL token（绕开被收紧的程序化密码登录）：
    先列已有 token、取未过期者；都没有则新建。失败（无网/凭证错）返回 None，
    调用方据此 fallback 到密码登录，不抛错。
    """
    if not (username and password):
        return None
    cached = _token_cache.get(username)
    if cached and not _token_expired(cached):
        return cached
    try:
        toks = _urs_request("GET", "/api/users/tokens", username, password) or []
        for t in toks:
            at = (t or {}).get("access_token")
            if at and not _token_expired(at):
                _token_cache[username] = at
                return at
        created = _urs_request("POST", "/api/users/token", username, password)
        at = (created or {}).get("access_token")
        if at:
            _token_cache[username] = at
            return at
    except Exception:
        return None
    return None


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


def get_earthdata_creds(creds: Dict[str, Any], auto_token: bool = True) -> Dict[str, str]:
    """
    提取 NASA Earthdata 凭证(HyP3 / ASF 共用)。

    当配置里只有 username/password、或 token 已过期时，自动用 URS API 换取一个有效
    token（auto_token=True，默认开）。这样下游一律走 token 登录，绕开被 Earthdata
    收紧的程序化密码登录。换取失败（无网/凭证错）则原样返回，不抛错。

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
    # token 缺失或已过期 → 用用户名/密码自动换取（HyP3 推荐的认证方式）
    if auto_token and out["username"] and out["password"] and \
            (not out["token"] or _token_expired(out["token"])):
        fresh = fetch_earthdata_token(out["username"], out["password"])
        if fresh:
            out["token"] = fresh
    if not (out["username"] and (out["password"] or out["token"])):
        raise CredentialsError(
            "nasa_earthdata 凭证不完整:需要 username + (password 或 token)。"
        )
    return out
