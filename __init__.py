"""
commons — 跨子系统公共库(geo-insar / geo-downloader 共享)

抽出自 geo-downloader/downloader/base.py 等,目的是让 geo-insar 等新子系统
直接复用认证、AOI 解析、网络出口、断点续传等基础设施,避免重复造轮子。

设计原则:
- geo-downloader 保持现状,复制而非引用(零回归风险)
- 后续 geo-downloader 切换到 commons 作为独立 PR
"""

__version__ = "0.1.0"
