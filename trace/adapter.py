"""存储后端抽象 —— TraceAdapter 接口 + JSONLAdapter 起步实现。

解耦核心（蓝图 §3）：Writer 只产出带 schema_version 的纯 dict，交给 Adapter 持久化。
Adapter 不理解语义，只负责"把一条记录存下来 / 按 trace_id 读回来"。
换后端 = 换 Adapter，Writer 与所有调用点零改动；历史迁移 = 一次批量 map ETL。

未来路径：JSONLAdapter（起步） → PostgresAdapter（规模化/JOIN） → ParquetAdapter（训练就绪）。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict, Iterator, List, Optional

# 与全系统 broker 约定一致：默认根用 /opt/deepexplor-services（符号链接到项目根）。
DEFAULT_TRACE_ROOT = os.environ.get(
    "DEEPEXPLOR_TRACE_ROOT", "/opt/deepexplor-services/_traces"
)


class TraceAdapter:
    """存储后端接口。子类实现 append / append_batch / query。"""

    def append(self, record: Dict) -> None:
        raise NotImplementedError

    def append_batch(self, records: List[Dict]) -> None:
        for r in records:
            self.append(r)

    def query(self, *, trace_id: Optional[str] = None,
              record_type: Optional[str] = None) -> Iterator[Dict]:
        raise NotImplementedError


class JSONLAdapter(TraceAdapter):
    """文件系统 JSONL 后端：每个 trace 一个文件 <root>/<trace_id>.jsonl，追加写。

    - 无 trace_id 的记录（理论上不应出现）归入 _orphan.jsonl。
    - 进程内按文件加锁，保证多线程追加不串行写坏。
    - 跨进程：append 模式 + 单行原子写，POSIX 下对小行 append 基本安全；
      大并发场景再迁移 Postgres（见 DEFAULT_TRACE_ROOT 注释）。
    """

    def __init__(self, root: str = DEFAULT_TRACE_ROOT):
        self.root = Path(root)
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _file_for(self, trace_id: Optional[str]) -> Path:
        name = trace_id if trace_id else "_orphan"
        return self.root / f"{name}.jsonl"

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lk = self._locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._locks[key] = lk
            return lk

    def append(self, record: Dict) -> None:
        trace_id = record.get("trace_id")
        path = self._file_for(trace_id)
        line = json.dumps(record, ensure_ascii=False)
        lk = self._lock_for(str(path))
        with lk:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def query(self, *, trace_id: Optional[str] = None,
              record_type: Optional[str] = None) -> Iterator[Dict]:
        if trace_id:
            files = [self._file_for(trace_id)]
        else:
            files = sorted(self.root.glob("*.jsonl")) if self.root.is_dir() else []
        for path in files:
            if not path.exists():
                continue
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except Exception:
                        continue
                    if record_type and rec.get("record_type") != record_type:
                        continue
                    yield rec

    def list_trace_ids(self) -> List[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob("*.jsonl")
                      if not p.stem.startswith("_"))
