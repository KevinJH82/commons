"""下游微服务内部鉴权:校验 BFF 注入的 X-Internal-Key,拒绝绕过 BFF 的直连。

用法(各 Flask 服务 app 创建后一行接入):
    from commons.internal_auth import init_internal_auth
    init_internal_auth(app)

策略(便于灰度,非破坏性):
- 环境变量 PORTAL_INTERNAL_KEY 未配置 → 不启用(打印告警),保持原开放行为;
- 已配置 → 除健康检查/OPTIONS 外,缺失或不匹配 X-Internal-Key 一律 403。
生产部署时给 BFF 与全部下游配同一个 PORTAL_INTERNAL_KEY 即全链生效。
"""
import hmac
import os

# 健康检查/根路径放行(供存活探针),OPTIONS 放行(CORS 预检)
_EXEMPT = ("/health", "/api/health", "/healthz", "/ping")


def init_internal_auth(app, exempt=()):
    key = os.environ.get("PORTAL_INTERNAL_KEY", "").strip()
    if not key:
        try:
            app.logger.warning("PORTAL_INTERNAL_KEY 未配置:内部鉴权未启用(本服务可被直连)")
        except Exception:
            print("[internal_auth] PORTAL_INTERNAL_KEY 未配置:内部鉴权未启用")
        return

    from flask import request, jsonify
    exempt_prefixes = _EXEMPT + tuple(exempt)

    @app.before_request
    def _require_internal_key():
        if request.method == "OPTIONS":
            return None
        path = request.path or ""
        if path == "/" or any(path.startswith(p) for p in exempt_prefixes):
            return None
        got = request.headers.get("X-Internal-Key", "")
        if not got or not hmac.compare_digest(got, key):
            return jsonify({"error": "forbidden: 必须经由门户(BFF)访问"}), 403
        return None
