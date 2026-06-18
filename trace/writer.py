"""TraceWriter —— 决策轨迹写入门面。

铁律（蓝图 §6）：
- **绝不影响业务**。所有 record_* 方法 fire-and-forget，内部异常一律吞掉只记日志，
  trace 写入失败不得让主流程报错。
- **绝不拖慢主流程**。默认 async：调用只把记录入队，后台线程批量落盘。

v用法::

    from commons.trace import get_writer
    w = get_writer()
    w.start_run(trace_id, aoi_name=..., mineral=..., bbox=...)
    sid = w.record_decision(trace_id, parent_span_id=None, service="geo-orchestrator",
                            run_id=None, decision_type=DECISION_SERVICE_ORCHESTRATION,
                            state={...}, decision={...}, rationale="...",
                            rationale_source="llm", alternatives_considered=[...],
                            decision_maker="deepseek")
    w.record_outcome(trace_id, parent_span_id=sid, service="geo-drill", run_id=rid,
                     outcome_type=OUTCOME_GROUND_TRUTH, ground_truth={...})
"""

from __future__ import annotations

import atexit
import logging
import queue
import threading
from typing import Dict, List, Optional, Tuple

from . import schema
from .adapter import JSONLAdapter, TraceAdapter
from .ids import new_span_id, utc_now_iso

logger = logging.getLogger("commons.trace")

_SENTINEL = object()


class TraceWriter:
    def __init__(self, adapter: Optional[TraceAdapter] = None, *, async_mode: bool = True):
        self.adapter = adapter or JSONLAdapter()
        self.async_mode = async_mode
        # 进程内登记：span_id -> trace_id，使 end_stage/close_run 无需调用方再传 trace_id。
        self._open_runs: Dict[str, str] = {}     # trace_id -> run span_id
        self._open_stages: Dict[str, Tuple[str, Dict]] = {}  # stage span_id -> (trace_id, payload)
        self._reg_guard = threading.Lock()

        self._q: "queue.Queue" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        if self.async_mode:
            self._worker = threading.Thread(target=self._drain, name="trace-writer",
                                            daemon=True)
            self._worker.start()
            atexit.register(self.flush)

    # ── 内部：落盘 ────────────────────────────────────────────
    def _emit(self, record: Dict) -> None:
        """把一条记录投递到后端。永不抛异常。"""
        try:
            if self.async_mode:
                self._q.put(record)
            else:
                self.adapter.append(record)
        except Exception as e:  # pragma: no cover - 防御性
            logger.debug("trace _emit 失败(忽略): %s", e)

    def _drain(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item is _SENTINEL:
                    self._q.task_done()
                    return
                self.adapter.append(item)
            except Exception as e:  # pragma: no cover
                logger.debug("trace 落盘失败(忽略): %s", e)
            finally:
                if item is not _SENTINEL:
                    self._q.task_done()

    def flush(self) -> None:
        """阻塞直到队列写空（供 atexit / 测试 / 关键节点调用）。"""
        try:
            if self.async_mode:
                self._q.join()
        except Exception:
            pass

    # ── Run ───────────────────────────────────────────────────
    def start_run(self, trace_id: str, *, aoi_name: str, mineral: str,
                  bbox: Optional[List[float]], service: str = "geo-orchestrator",
                  area_km2: Optional[float] = None, roi_context_ref: Optional[str] = None,
                  requested_by: Optional[str] = None,
                  trace_origin: str = schema.ORIGIN_SELF) -> Optional[str]:
        try:
            rec = schema.build_run(trace_id, service=service, aoi_name=aoi_name,
                                   mineral=mineral, bbox=bbox, area_km2=area_km2,
                                   roi_context_ref=roi_context_ref,
                                   requested_by=requested_by, trace_origin=trace_origin)
            with self._reg_guard:
                self._open_runs[trace_id] = rec["span_id"]
            self._emit(rec)
            return rec["span_id"]
        except Exception as e:
            logger.debug("start_run 失败(忽略): %s", e)
            return None

    def close_run(self, trace_id: str, *, closed_by: str,
                  service: str = "geo-orchestrator") -> None:
        try:
            with self._reg_guard:
                span_id = self._open_runs.get(trace_id)
            rec = schema.build_run(trace_id, service=service, aoi_name="", mineral="",
                                   bbox=None, status="closed", closed_by=closed_by,
                                   span_id=span_id)
            self._emit(rec)
        except Exception as e:
            logger.debug("close_run 失败(忽略): %s", e)

    # ── Stage ─────────────────────────────────────────────────
    def begin_stage(self, trace_id: str, *, phase, name: str, services: List[str],
                    service: str = "geo-orchestrator",
                    plan_ref: Optional[str] = None) -> Optional[str]:
        try:
            span_id = new_span_id()
            rec = schema.build_stage(trace_id, span_id=span_id, service=service,
                                     phase=phase, name=name, services=services,
                                     plan_ref=plan_ref, status="running",
                                     started_at=utc_now_iso())
            with self._reg_guard:
                self._open_stages[span_id] = (trace_id, rec["payload"])
            self._emit(rec)
            return span_id
        except Exception as e:
            logger.debug("begin_stage 失败(忽略): %s", e)
            return None

    def end_stage(self, stage_span_id: Optional[str], *, status: str,
                  skip: bool = False, skip_reason: str = "",
                  service: str = "geo-orchestrator") -> None:
        if not stage_span_id:
            return
        try:
            with self._reg_guard:
                entry = self._open_stages.pop(stage_span_id, None)
            if entry is None:
                return
            trace_id, payload = entry
            rec = schema.build_stage(
                trace_id, span_id=stage_span_id, service=service,
                phase=payload.get("phase"), name=payload.get("name", ""),
                services=payload.get("services", []), plan_ref=payload.get("plan_ref"),
                status=status, skip=skip, skip_reason=skip_reason,
                started_at=payload.get("started_at"), ended_at=utc_now_iso())
            self._emit(rec)
        except Exception as e:
            logger.debug("end_stage 失败(忽略): %s", e)

    # ── Decision（核心可训练样本）─────────────────────────────
    def record_decision(self, trace_id: str, *, parent_span_id: Optional[str],
                        service: str, run_id: Optional[str], decision_type: str,
                        state: Dict, decision: Dict, rationale: str,
                        rationale_source: str,
                        alternatives_considered: Optional[List[Dict]] = None,
                        decision_maker: str = "",
                        outcome_ref: Optional[str] = None) -> Optional[str]:
        try:
            rec = schema.build_decision(
                trace_id, parent_span_id=parent_span_id, service=service, run_id=run_id,
                decision_type=decision_type, state=state, decision=decision,
                rationale=rationale, rationale_source=rationale_source,
                alternatives_considered=alternatives_considered,
                decision_maker=decision_maker, outcome_ref=outcome_ref)
            self._emit(rec)
            return rec["span_id"]
        except Exception as e:
            logger.debug("record_decision 失败(忽略): %s", e)
            return None

    # ── Outcome / ground-truth ────────────────────────────────
    def record_outcome(self, trace_id: str, *, parent_span_id: Optional[str],
                       service: str, run_id: Optional[str], outcome_type: str,
                       products: Optional[Dict] = None, metrics: Optional[Dict] = None,
                       ground_truth: Optional[Dict] = None) -> Optional[str]:
        try:
            rec = schema.build_outcome(
                trace_id, parent_span_id=parent_span_id, service=service, run_id=run_id,
                outcome_type=outcome_type, products=products, metrics=metrics,
                ground_truth=ground_truth)
            self._emit(rec)
            return rec["span_id"]
        except Exception as e:
            logger.debug("record_outcome 失败(忽略): %s", e)
            return None

    # ── trace_id 血缘继承工具（供 broker 调用）──────────────────
    @staticmethod
    def inherit_trace_id(upstream_metadatas: List[Dict]) -> Tuple[Optional[str], List[str]]:
        """从上游 metadata 列表推导本次运行应继承的 trace_id。

        返回 (主 trace_id, linked_trace_ids[])：
        - 主：覆盖证据最多/时间最近者（这里按出现顺序取首个有 trace_id 的；
          调用方若已按 created_at 排序，则天然"最近优先"）。
        - linked：去重后的全部 trace_id（多源融合时记录所有来源）。
        """
        seen: List[str] = []
        for md in upstream_metadatas or []:
            tid = (md or {}).get("trace_id")
            if tid and tid not in seen:
                seen.append(tid)
        if not seen:
            return None, []
        return seen[0], seen


# ── 进程级默认单例 ─────────────────────────────────────────────
_default_writer: Optional[TraceWriter] = None
_default_guard = threading.Lock()


def get_writer() -> TraceWriter:
    """获取进程级默认 TraceWriter（JSONLAdapter + async）。"""
    global _default_writer
    if _default_writer is None:
        with _default_guard:
            if _default_writer is None:
                _default_writer = TraceWriter()
    return _default_writer
