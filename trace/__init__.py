"""commons.trace —— 决策轨迹库 (Reasoning Trace Store)

为未来 geo-LLM 积累端到端「状态→决策→依据→结果」训练样本。全系统共用。

公开 API::

    from commons.trace import get_writer, new_trace_id
    from commons.trace import (
        DECISION_SERVICE_ORCHESTRATION, DECISION_ORE_JUDGEMENT,
        OUTCOME_GROUND_TRUTH, ORIGIN_INHERITED, ...
    )

设计见架构蓝图：trace_id 沿数据血缘传播（P1）/ orchestrator 调用注入（P2），
Writer 与存储后端解耦（JSONL 起步，可换 Postgres/Parquet）。
"""

from .ids import new_trace_id, new_span_id, is_trace_id, utc_now_iso
from .adapter import TraceAdapter, JSONLAdapter, DEFAULT_TRACE_ROOT
from .writer import TraceWriter, get_writer
from .lineage import (
    resolve_trace_id, stamp_metadata, entry_matches_trace, filter_by_trace_id,
    entry_matches_tenant, filter_by_tenant,
)
from .schema import (
    SCHEMA_VERSION,
    RECORD_RUN, RECORD_STAGE, RECORD_DECISION, RECORD_OUTCOME,
    DECISION_ROI_TO_MINERAL_FAMILY, DECISION_SENSOR_SELECTION,
    DECISION_SERVICE_ORCHESTRATION, DECISION_DEPOSIT_TYPE_INFERENCE,
    DECISION_EVIDENCE_FUSION_METHOD, DECISION_DEPTH_GATING,
    DECISION_PROSPECTIVITY_TO_TARGETS, DECISION_HOLE_SITING,
    DECISION_ORE_JUDGEMENT, DECISION_CLOSED_LOOP_RELABEL,
    OUTCOME_PRODUCT, OUTCOME_METRIC, OUTCOME_GROUND_TRUTH,
    ORIGIN_EXPLICIT, ORIGIN_INHERITED, ORIGIN_SELF,
)

__all__ = [
    "new_trace_id", "new_span_id", "is_trace_id", "utc_now_iso",
    "TraceAdapter", "JSONLAdapter", "DEFAULT_TRACE_ROOT",
    "TraceWriter", "get_writer",
    "resolve_trace_id", "stamp_metadata", "entry_matches_trace", "filter_by_trace_id",
    "entry_matches_tenant", "filter_by_tenant",
    "SCHEMA_VERSION",
    "RECORD_RUN", "RECORD_STAGE", "RECORD_DECISION", "RECORD_OUTCOME",
    "DECISION_ROI_TO_MINERAL_FAMILY", "DECISION_SENSOR_SELECTION",
    "DECISION_SERVICE_ORCHESTRATION", "DECISION_DEPOSIT_TYPE_INFERENCE",
    "DECISION_EVIDENCE_FUSION_METHOD", "DECISION_DEPTH_GATING",
    "DECISION_PROSPECTIVITY_TO_TARGETS", "DECISION_HOLE_SITING",
    "DECISION_ORE_JUDGEMENT", "DECISION_CLOSED_LOOP_RELABEL",
    "OUTCOME_PRODUCT", "OUTCOME_METRIC", "OUTCOME_GROUND_TRUTH",
    "ORIGIN_EXPLICIT", "ORIGIN_INHERITED", "ORIGIN_SELF",
]
