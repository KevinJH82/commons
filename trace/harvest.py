"""被动采集器 Trace Harvester —— 把碎片拼成端到端轨迹，并导出 geo-LLM 训练样本。

三件事（见架构蓝图 §4/§5）：
1. reconstruct(): 从轨迹库 JSONL 把扁平事件流重组成 Run→Stage→Decision→Outcome 树。
2. harvest_from_brokers(): 扫描各服务已落盘的 metadata（经 broker），按 trace_id 把
   产物补成 Outcome 记录写回轨迹库 —— 零侵入地覆盖尚未主动插桩的服务。
3. export_samples(): 产出三类训练样本
   - sft_decisions.jsonl  决策点级监督样本 (state→decision+rationale)
   - trajectories.jsonl   端到端轨迹（含 alternatives，可做偏好学习）
   - grounded_pairs.jsonl 闭环四元组（model3d 预测 × drill 实钻 ground-truth）—— 最高价值

被动采集铁律：只读扫描，绝不修改各服务产物；对主流程零开销（离线运行）。

CLI::
    python -m commons.trace.harvest --list
    python -m commons.trace.harvest --harvest            # 扫 broker 补产物 Outcome
    python -m commons.trace.harvest --export OUT_DIR     # 导出训练样本
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Dict, Iterable, List, Optional

from .adapter import JSONLAdapter, TraceAdapter
from .schema import (RECORD_RUN, RECORD_STAGE, RECORD_DECISION, RECORD_OUTCOME,
                     OUTCOME_PRODUCT, OUTCOME_GROUND_TRUTH,
                     DECISION_PROSPECTIVITY_TO_TARGETS, DECISION_HOLE_SITING)


# ── 1) 重组：扁平事件流 → 轨迹树 ──────────────────────────────
def reconstruct(records: Iterable[Dict]) -> Dict:
    """把同一 trace 的记录重组成 {trace_id, run, stages[], decisions[], outcomes[], closed}。

    run 取最后一条 run 记录（status=closed 优先反映闭环）。
    """
    recs = list(records)
    run = None
    closed_by = None
    stages: Dict[str, Dict] = {}
    decisions: List[Dict] = []
    outcomes: List[Dict] = []
    trace_id = None
    for r in recs:
        trace_id = trace_id or r.get("trace_id")
        rt = r.get("record_type")
        if rt == RECORD_RUN:
            p = r.get("payload", {})
            if run is None:
                run = dict(p)
            if p.get("status") == "closed":
                closed_by = p.get("closed_by")
            # 用非空字段补全（close_run 的记录 aoi/mineral 为空）
            for k, v in p.items():
                if v and not run.get(k):
                    run[k] = v
        elif rt == RECORD_STAGE:
            sid = r.get("span_id")
            # 同 span_id 后到的（end_stage）覆盖先到的（begin_stage）
            stages[sid] = {**stages.get(sid, {}), **r.get("payload", {}), "span_id": sid}
        elif rt == RECORD_DECISION:
            decisions.append({**r.get("payload", {}), "span_id": r.get("span_id"),
                              "service": r.get("service"), "ts": r.get("ts")})
        elif rt == RECORD_OUTCOME:
            outcomes.append({**r.get("payload", {}), "span_id": r.get("span_id"),
                             "parent_span_id": r.get("parent_span_id"),
                             "service": r.get("service"), "ts": r.get("ts")})
    if run is not None and closed_by:
        run["status"] = "closed"
        run["closed_by"] = closed_by
    return {
        "trace_id": trace_id,
        "run": run,
        "stages": sorted(stages.values(), key=lambda s: (s.get("phase") if s.get("phase") is not None else 0)),
        "decisions": decisions,
        "outcomes": outcomes,
        "closed": bool(closed_by),
    }


def iter_assembled(adapter: Optional[TraceAdapter] = None) -> Iterable[Dict]:
    """遍历轨迹库，逐 trace 产出重组后的轨迹。"""
    adapter = adapter or JSONLAdapter()
    if isinstance(adapter, JSONLAdapter):
        ids = adapter.list_trace_ids()
        for tid in ids:
            yield reconstruct(adapter.query(trace_id=tid))
    else:  # 通用：先按 trace_id 分组
        groups: Dict[str, List[Dict]] = defaultdict(list)
        for r in adapter.query():
            groups[r.get("trace_id")].append(r)
        for tid, recs in groups.items():
            yield reconstruct(recs)


# ── 2) 从 broker 扫描补产物 Outcome（覆盖未插桩服务）────────────
# (broker 模块名, scan 函数名, 服务名, 默认根的 roots key)
_BROKER_SOURCES = [
    ("commons.analyser_broker", "scan_alteration_outputs", "geo-analyser", "analyser"),
    ("commons.model3d_broker", "scan_model3d_outputs", "geo-model3d", "model3d"),
    ("commons.drill_broker", "scan_drill_outputs", "geo-drill", "drill"),
    ("commons.structural_broker", "scan_structural_aois", "geo-stru", "structural"),
    ("commons.insar_broker", "scan_insar_outputs", "geo-insar", "insar"),
    ("commons.geophys_broker", "scan_geophys_outputs", "geo-geophys", "geophys"),
    ("commons.geochem_broker", "scan_geochem_outputs", "geo-geochem", "geochem"),
    ("commons.datacolle_broker", "scan_datacolle_outputs", "data-colle", "datacolle"),
    ("commons.exploration_broker", "scan_exploration_outputs", "geo-exploration", "exploration"),
]


def harvest_from_brokers(roots: Optional[Dict[str, str]] = None,
                         adapter: Optional[TraceAdapter] = None,
                         emit: bool = True) -> List[Dict]:
    """扫描 trace 感知的 broker，把带 trace_id 的产物补成 OUTCOME_PRODUCT 记录。

    只处理 entry 自带 trace_id 者（未带 trace_id 的历史存量留给 bbox 兜底，另行处理）。
    返回新建的 outcome 记录列表；emit=True 时写回轨迹库。
    """
    from .writer import get_writer
    roots = roots or {}
    new_records: List[Dict] = []
    writer = get_writer() if emit else None

    for mod_name, fn_name, service, _key in _BROKER_SOURCES:
        try:
            mod = __import__(mod_name, fromlist=[fn_name])
            scan = getattr(mod, fn_name)
        except Exception:
            continue
        try:
            entries = scan() if not roots.get(_key) else scan(roots[_key])
        except Exception:
            continue
        for e in entries:
            tid = e.get("trace_id")
            if not tid:
                continue
            products = e.get("products") or {}
            # 各 broker stats 键不一：model_stats/structural_stats/stats/statistics
            metrics = (e.get("model_stats") or e.get("structural_stats")
                       or e.get("stats") or e.get("statistics") or {})
            run_id = e.get("run_id")
            if writer is not None:
                sid = writer.record_outcome(
                    tid, parent_span_id=None, service=service, run_id=run_id,
                    outcome_type=OUTCOME_PRODUCT, products=products,
                    metrics=metrics if isinstance(metrics, dict) else {})
                new_records.append({"trace_id": tid, "service": service,
                                    "span_id": sid, "products": list(products)})
    if writer is not None:
        writer.flush()
    return new_records


# ── 3) 导出 geo-LLM 训练样本 ─────────────────────────────────
def export_samples(out_dir: str, adapter: Optional[TraceAdapter] = None) -> Dict[str, int]:
    """导出三类样本到 out_dir，返回各类条数。"""
    adapter = adapter or JSONLAdapter()
    os.makedirs(out_dir, exist_ok=True)
    n_sft = n_traj = n_pair = 0

    sft_f = open(os.path.join(out_dir, "sft_decisions.jsonl"), "w", encoding="utf-8")
    traj_f = open(os.path.join(out_dir, "trajectories.jsonl"), "w", encoding="utf-8")
    pair_f = open(os.path.join(out_dir, "grounded_pairs.jsonl"), "w", encoding="utf-8")
    try:
        for asm in iter_assembled(adapter):
            tid = asm.get("trace_id")
            # (a) 决策点 SFT 样本：state → {decision, rationale}
            for d in asm["decisions"]:
                sft_f.write(json.dumps({
                    "trace_id": tid,
                    "decision_type": d.get("decision_type"),
                    "input": d.get("state", {}),
                    "target": {"decision": d.get("decision", {}),
                               "rationale": d.get("rationale", "")},
                    "alternatives": d.get("alternatives_considered", []),
                    "rationale_source": d.get("rationale_source"),
                    "decision_maker": d.get("decision_maker"),
                }, ensure_ascii=False) + "\n")
                n_sft += 1

            # (b) 端到端轨迹样本
            traj_f.write(json.dumps({
                "trace_id": tid,
                "run": asm.get("run"),
                "closed": asm.get("closed"),
                "decisions": [{"decision_type": d.get("decision_type"),
                               "state": d.get("state"), "decision": d.get("decision"),
                               "rationale": d.get("rationale"),
                               "alternatives": d.get("alternatives_considered")}
                              for d in asm["decisions"]],
                "outcomes": [{"service": o.get("service"),
                              "outcome_type": o.get("outcome_type"),
                              "ground_truth": o.get("ground_truth"),
                              "products": list(o.get("products") or {})}
                             for o in asm["outcomes"]],
            }, ensure_ascii=False) + "\n")
            n_traj += 1

            # (c) 闭环四元组：drill ground-truth × 上游预测（最高价值）
            gts = [o for o in asm["outcomes"]
                   if o.get("outcome_type") == OUTCOME_GROUND_TRUTH and o.get("ground_truth")]
            if gts:
                pred = _prediction_context(asm)
                for o in gts:
                    pair_f.write(json.dumps({
                        "trace_id": tid,
                        "state": pred,                       # 决策时的预测上下文
                        "action": "drill_hole",
                        "ground_truth": o.get("ground_truth"),  # ore/barren 实钻真值
                    }, ensure_ascii=False) + "\n")
                    n_pair += 1
    finally:
        sft_f.close(); traj_f.close(); pair_f.close()
    return {"sft_decisions": n_sft, "trajectories": n_traj, "grounded_pairs": n_pair}


def _prediction_context(asm: Dict) -> Dict:
    """从轨迹里抽取 drill 之前的预测上下文（D7 靶点 / D8 布孔 决策的 state）。"""
    ctx = {"mineral": (asm.get("run") or {}).get("mineral"),
           "aoi_name": (asm.get("run") or {}).get("aoi_name")}
    for d in asm.get("decisions", []):
        if d.get("decision_type") in (DECISION_PROSPECTIVITY_TO_TARGETS, DECISION_HOLE_SITING):
            ctx[d["decision_type"]] = {"state": d.get("state"), "decision": d.get("decision")}
    # 也带上各服务产物清单（证据来源）
    ctx["products"] = sorted({p for o in asm.get("outcomes", [])
                              for p in (o.get("products") or {})})
    return ctx


# ── CLI ───────────────────────────────────────────────────────
def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="决策轨迹库 Harvester")
    ap.add_argument("--root", default=None, help="轨迹库根目录(覆盖 DEEPEXPLOR_TRACE_ROOT)")
    ap.add_argument("--list", action="store_true", help="列出所有 trace 及其闭环状态")
    ap.add_argument("--harvest", action="store_true", help="扫 broker 把产物补成 Outcome")
    ap.add_argument("--export", metavar="OUT_DIR", help="导出训练样本到目录")
    args = ap.parse_args(argv)

    adapter = JSONLAdapter(args.root) if args.root else JSONLAdapter()

    if args.harvest:
        recs = harvest_from_brokers(adapter=adapter, emit=True)
        print(f"已补 {len(recs)} 条产物 Outcome 记录")

    if args.list:
        n = 0
        for asm in iter_assembled(adapter):
            run = asm.get("run") or {}
            flag = "✓闭环" if asm.get("closed") else " 进行中"
            print(f"[{flag}] {asm['trace_id']}  矿种={run.get('mineral','?')} "
                  f"区={run.get('aoi_name','?')}  决策={len(asm['decisions'])} "
                  f"结果={len(asm['outcomes'])}")
            n += 1
        print(f"共 {n} 条 trace")

    if args.export:
        counts = export_samples(args.export, adapter)
        print(f"导出至 {args.export}: {counts}")

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
