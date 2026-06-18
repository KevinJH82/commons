"""决策轨迹 schema —— 记录信封 + 各层 payload 构造器。

设计要点（见架构蓝图 §2）：
- 每条记录是扁平的事件流（一条 JSONL = 一行），靠 trace_id / parent_span_id 读取时重组成树。
- 重产物（GeoTIFF/NetCDF/PNG）只存路径引用，绝不内联。
- 每条记录带 schema_version，使 schema 与存储后端解耦：换后端只是换 Adapter + 一次 ETL。

四层：Run（一次 ROI 请求）→ Stage（5 阶段）→ Decision（决策点，可训练样本单元）→ Outcome（结果/ground-truth）。

本模块只产出**纯 dict**，不做任何 I/O；持久化交给 adapter.TraceAdapter。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .ids import new_span_id, utc_now_iso

SCHEMA_VERSION = "trace/1.0"

# ── record_type 枚举 ──────────────────────────────────────────
RECORD_RUN = "run"
RECORD_STAGE = "stage"
RECORD_DECISION = "decision"
RECORD_OUTCOME = "outcome"

# ── decision_type 枚举（逐阶段决策点清单，见蓝图 §2.6）──────────
# D1..D10 —— 每个都是一个 (state → decision → rationale → outcome) 可训练样本
DECISION_ROI_TO_MINERAL_FAMILY = "roi_to_mineral_family"      # D1
DECISION_SENSOR_SELECTION = "sensor_selection"                # D2
DECISION_SERVICE_ORCHESTRATION = "service_orchestration"      # D3 (LLM)
DECISION_DEPOSIT_TYPE_INFERENCE = "deposit_type_inference"    # D4 (LLM)
DECISION_EVIDENCE_FUSION_METHOD = "evidence_fusion_method"    # D5
DECISION_DEPTH_GATING = "depth_gating"                        # D6
DECISION_PROSPECTIVITY_TO_TARGETS = "prospectivity_to_targets"  # D7
DECISION_HOLE_SITING = "hole_siting"                          # D8
DECISION_ORE_JUDGEMENT = "ore_judgement"                      # D9 (闭环金标签)
DECISION_CLOSED_LOOP_RELABEL = "closed_loop_relabel"          # D10

# ── outcome_type 枚举 ─────────────────────────────────────────
OUTCOME_PRODUCT = "product"
OUTCOME_METRIC = "metric"
OUTCOME_GROUND_TRUTH = "ground_truth"

# ── trace_origin 枚举 ─────────────────────────────────────────
ORIGIN_EXPLICIT = "explicit"      # 调用方显式传入（P2 orchestrator 注入 / 前端透传）
ORIGIN_INHERITED = "inherited"    # 沿数据血缘从上游 metadata 继承
ORIGIN_SELF = "self"              # 单服务独立跑，自生成兜底


def _envelope(record_type: str, trace_id: str, *, service: str,
              run_id: Optional[str], parent_span_id: Optional[str],
              span_id: Optional[str] = None) -> Dict:
    """通用记录信封（蓝图 §2.1）。payload 由调用方填充。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": record_type,
        "trace_id": trace_id,
        "span_id": span_id or new_span_id(),
        "parent_span_id": parent_span_id,
        "service": service,
        "run_id": run_id,
        "ts": utc_now_iso(),
        "payload": {},
    }


def build_run(trace_id: str, *, service: str, aoi_name: str, mineral: str,
              bbox: Optional[List[float]], area_km2: Optional[float] = None,
              roi_context_ref: Optional[str] = None, requested_by: Optional[str] = None,
              trace_origin: str = ORIGIN_SELF, status: str = "open",
              closed_by: Optional[str] = None, span_id: Optional[str] = None) -> Dict:
    rec = _envelope(RECORD_RUN, trace_id, service=service, run_id=None,
                    parent_span_id=None, span_id=span_id)
    rec["payload"] = {
        "aoi_name": aoi_name,
        "mineral": mineral,
        "bbox": [float(v) for v in bbox] if bbox else None,
        "area_km2": area_km2,
        "roi_context_ref": roi_context_ref,
        "requested_by": requested_by,     # 调用方负责脱敏
        "trace_origin": trace_origin,
        "status": status,                 # open | closed
        "closed_by": closed_by,           # drill_feedback | report | None
    }
    return rec


def build_stage(trace_id: str, *, span_id: str, service: str, phase, name: str,
                services: List[str], plan_ref: Optional[str] = None,
                status: str = "running", skip: bool = False, skip_reason: str = "",
                started_at: Optional[str] = None, ended_at: Optional[str] = None) -> Dict:
    rec = _envelope(RECORD_STAGE, trace_id, service=service, run_id=None,
                    parent_span_id=None, span_id=span_id)
    rec["payload"] = {
        "phase": phase,
        "name": name,
        "services": services,
        "plan_ref": plan_ref,
        "status": status,                 # running | ok | degraded | failed
        "skip": skip,
        "skip_reason": skip_reason,
        "started_at": started_at,
        "ended_at": ended_at,
    }
    return rec


def build_decision(trace_id: str, *, parent_span_id: Optional[str], service: str,
                   run_id: Optional[str], decision_type: str, state: Dict,
                   decision: Dict, rationale: str, rationale_source: str,
                   alternatives_considered: Optional[List[Dict]] = None,
                   decision_maker: str = "", outcome_ref: Optional[str] = None,
                   span_id: Optional[str] = None) -> Dict:
    """决策点 —— 轨迹库的核心可训练样本单元（蓝图 §2.4）。

    state                  : 决策时上下文（结构化摘要 + 重数据 context_refs 引用）
    decision               : 系统/Agent 做出的选择 {action, params}
    rationale              : 决策依据文本（来自 decision_rationale / reason 字段）
    rationale_source       : llm | deterministic | rule | ml
    alternatives_considered: [{action, rejected_reason}] —— 用于偏好学习 (DPO/RLHF)
    """
    rec = _envelope(RECORD_DECISION, trace_id, service=service, run_id=run_id,
                    parent_span_id=parent_span_id, span_id=span_id)
    rec["payload"] = {
        "decision_type": decision_type,
        "state": state or {},
        "decision": decision or {},
        "rationale": rationale or "",
        "rationale_source": rationale_source,
        "alternatives_considered": alternatives_considered or [],
        "decision_maker": decision_maker,
        "outcome_ref": outcome_ref,
    }
    return rec


def build_outcome(trace_id: str, *, parent_span_id: Optional[str], service: str,
                  run_id: Optional[str], outcome_type: str,
                  products: Optional[Dict] = None, metrics: Optional[Dict] = None,
                  ground_truth: Optional[Dict] = None,
                  span_id: Optional[str] = None) -> Dict:
    """结果层。products 仅存路径引用；ground_truth 仅 drill 闭环填充（稀缺金标签）。"""
    rec = _envelope(RECORD_OUTCOME, trace_id, service=service, run_id=run_id,
                    parent_span_id=parent_span_id, span_id=span_id)
    rec["payload"] = {
        "outcome_type": outcome_type,     # product | metric | ground_truth
        "products": products or {},       # {name: 相对/绝对路径}
        "metrics": metrics or {},
        "ground_truth": ground_truth,     # {hole_id, outcome, element, max_grade, cutoff}
    }
    return rec
