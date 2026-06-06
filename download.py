"""
download.py — 带断点续传 + stall 检测的 HTTP 下载工具

抽出自 geo-downloader/downloader/base.py:download_with_resume。
特性:
- .part 临时文件 + Range 续传
- stall 检测(120s 无数据自动断开重连)
- 最多 10 次重试,指数退避
- 完成后大小校验(防止截断)
"""

import threading
import time
import random
from pathlib import Path
from typing import Dict, Optional

from .network import resolve_proxies

_STALL_TIMEOUT = 120


def _iter_content_with_stall_guard(resp, chunk_size, stall_timeout=_STALL_TIMEOUT):
    """包装 resp.iter_content,stall_timeout 秒无数据则主动断开。"""
    import queue
    _SENTINEL = object()
    q = queue.Queue(maxsize=2)

    def _producer():
        try:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                q.put(chunk)
            q.put(_SENTINEL)
        except Exception as e:
            q.put(e)

    t = threading.Thread(target=_producer, daemon=True)
    t.start()
    try:
        while True:
            try:
                item = q.get(timeout=stall_timeout)
            except queue.Empty:
                try:
                    resp.close()
                except Exception:
                    pass
                import requests as _req
                raise _req.ConnectionError(
                    f"下载停滞超过 {stall_timeout}s,主动断开重连"
                )
            if item is _SENTINEL:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    except GeneratorExit:
        try:
            resp.close()
        except Exception:
            pass


def download_with_resume(
    session_or_requests,
    url: str,
    dest: Path,
    desc: str = "",
    chunk_size: int = 1024 * 1024,
    timeout: int = 600,
    headers: Optional[Dict] = None,
    proxies=None,
) -> Path:
    """
    带断点续传的文件下载。

    Parameters
    ----------
    session_or_requests : requests.Session 或 requests 模块
    url                 : 下载地址
    dest                : 目标文件路径(不含 .part 后缀)
    desc                : 进度描述
    proxies             : "auto" / None / dict — 走 resolve_proxies()
    """
    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    if dest.exists():
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    req_headers = dict(headers or {})

    max_net_retries = 10
    bar = None
    resolved_proxies = resolve_proxies(session_or_requests, proxies if proxies is not None else "auto")

    try:
        for net_attempt in range(max_net_retries):
            try:
                existing_size = part.stat().st_size if part.exists() else 0

                if existing_size > 0:
                    req_headers["Range"] = f"bytes={existing_size}-"
                elif "Range" in req_headers:
                    del req_headers["Range"]

                resp = session_or_requests.get(
                    url, headers=req_headers, stream=True,
                    timeout=(30, min(timeout, 120)),
                    proxies=resolved_proxies,
                )

                # 服务端不支持 Range(返回 200),从头重下
                if existing_size > 0 and resp.status_code == 200:
                    existing_size = 0
                    part.unlink(missing_ok=True)

                resp.raise_for_status()

                total = int(resp.headers.get("content-length", 0))
                if existing_size > 0:
                    total += existing_size

                mode = "ab" if existing_size > 0 else "wb"

                if bar is None and tqdm is not None:
                    bar = tqdm(
                        total=total or None,
                        initial=existing_size,
                        unit="B", unit_scale=True,
                        desc=f"      {desc[:50]}", leave=False,
                    )

                with open(part, mode) as f:
                    for chunk in _iter_content_with_stall_guard(resp, chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                        if bar is not None:
                            bar.update(len(chunk))

                if total > 0 and part.stat().st_size != total:
                    actual = part.stat().st_size
                    part.unlink(missing_ok=True)
                    raise ConnectionError(
                        f"文件大小不符: 预期 {total} B,实际 {actual} B,疑似截断"
                    )

                break  # 成功

            except Exception as e:
                import requests as _requests
                import requests.exceptions as _req_exc
                _net_errors = (
                    _requests.ConnectionError,
                    _requests.Timeout,
                    _req_exc.ChunkedEncodingError,
                    ConnectionResetError,
                    TimeoutError,
                )
                is_net_err = isinstance(e, _net_errors)
                if not is_net_err and isinstance(e, OSError):
                    is_net_err = True

                if is_net_err and net_attempt < max_net_retries - 1:
                    wait = min(2 ** net_attempt + random.uniform(0, 1), 120)
                    print(f"      [重试 {net_attempt + 1}/{max_net_retries}] 网络中断,{wait:.0f}s 后重连... ({e})")
                    time.sleep(wait)
                else:
                    raise
    finally:
        if bar is not None:
            bar.close()

    part.rename(dest)
    return dest
