"""
base_downloader.py — BaseDownloader 抽象基类

抽出自 geo-downloader/downloader/base.py:BaseDownloader,精简为
geo-insar 需要的接口(去掉了原版裁剪、镶嵌等光学专属逻辑,这些不适用 InSAR)。
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Tuple, Optional, List, Dict, Any
from datetime import datetime


class BaseDownloader(ABC):
    """
    所有下载器的基类。

    子类必须实现:
    - search(): 搜索符合条件的影像/产品
    - download(): 下载到本地
    """

    PLATFORM_NAME: str = "unknown"
    REQUIRES_AUTH: bool = True

    def __init__(
        self,
        credentials: Optional[Dict[str, str]] = None,
        output_dir: str = "./downloads",
    ):
        self.credentials = credentials or {}
        self.output_dir = Path(output_dir)

    def get_save_dir(self, area_name: str) -> Path:
        """返回 {output_dir}/{area_name}/{platform}/,自动创建。"""
        d = self.output_dir / area_name / self.PLATFORM_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    @abstractmethod
    def search(
        self,
        bbox: Tuple[float, float, float, float],
        start_date: str,
        end_date: str,
        **kwargs,
    ) -> List[Any]:
        """搜索符合条件的影像/产品。"""

    @abstractmethod
    def download(
        self,
        search_results: List[Any],
        save_dir: Path,
        **kwargs,
    ) -> List[Path]:
        """下载到本地。"""

    @staticmethod
    def _validate_date(date_str: str) -> str:
        """验证日期格式 YYYY-MM-DD。"""
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
            return date_str
        except ValueError:
            raise ValueError(f"日期格式错误: '{date_str}',应为 YYYY-MM-DD")
