"""
network.py — OpenVPN-aware 代理管理

抽出自 geo-downloader/downloader/base.py:_resolve_proxies。
geo-downloader 项目用 OpenVPN 做系统级路由出口,应用层不再设代理。
"""


def resolve_proxies(session_or_requests, override="auto"):
    """
    统一解析 requests.get/post 的 proxies 参数。

    - override != "auto": 调用方显式指定(None 或 dict),原样返回
    - override == "auto" 且 session.trust_env=False: 视为"绕过代理"意图,
      返回空字符串字典(requests 推荐的彻底禁用代理写法)
    - 其余情况: 返回 None,走 requests 默认行为(读 HTTP_PROXY 环境变量或直连)

    出口走 OpenVPN(系统级路由),requests 不需要应用层代理。
    """
    if override != "auto":
        return override
    if getattr(session_or_requests, "trust_env", True) is False:
        return {"http": "", "https": ""}
    return None
