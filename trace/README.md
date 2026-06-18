# commons.trace —— 决策轨迹库 (Reasoning Trace Store)

为未来 geo-LLM 积累端到端「状态→决策→依据→结果」训练样本。全系统共用，放在 `commons/`。

设计蓝图见 `~/.claude/plans/roi-geo-llm-steady-rainbow.md`。本目录是 **Phase A** 落地。

## 它解决什么

11 个服务各有各的 `run_id`，靠 bbox 模糊关联，**没有一根线把"同一次 ROI 请求"从 orchestrator 串到 drill 反馈**。本模块用一个贯穿全链的 `trace_id` 把碎片拼成可训练的决策轨迹，其中 **drill 见矿结果 (ore/barren) 是平台唯一的物理真值**，是最稀缺的闭环金标签。

## 四层 Schema（`schema.py`）

每条记录是一行 JSONL，靠 `trace_id`/`parent_span_id` 读取时重组成树。重产物（GeoTIFF/NetCDF）**只存路径引用**。

```
Run（一次 ROI 请求）→ Stage（5 阶段）→ Decision（决策点=可训练样本）→ Outcome（结果/ground-truth）
```

决策点清单 D1–D10（`decision_type` 枚举）：ROI→矿种族、传感器选择、任务编排(LLM)、矿床推断(LLM)、证据融合方法、深度门控、靶点、布孔、**见矿判定(金标签)**、闭环回灌。

## trace_id 怎么传（`lineage.py`）

优先级（同一套机制兼容 P1/P2）：
1. **显式入参** `trace_id` — P1 前端透传 / P2 orchestrator 调度注入（主路径）。
2. **血缘继承** — broker 发现上游产物时从其 `metadata.json` 读 `trace_id`（P1 核心）。
3. **自生成兜底** — 单服务独立跑。

## 服务接入（两步，全部容错）

**写盘时** 给 metadata/manifest 注入三键（已接入 model3d / analyser / drill 三个 writer，是范式样板）：

```python
from commons.trace import stamp_metadata
stamp_metadata(meta, explicit_trace_id=trace_id,        # 入参，可 None
               upstream_metadatas=upstream_md_list)      # broker 命中的上游 metadata
# meta 现含 trace_id / linked_trace_ids / trace_origin
```

**记录决策/结果**（高价值点主动插桩，已接入 orchestrator D1/D2/D3、drill D9）：

```python
from commons.trace import get_writer, DECISION_ORE_JUDGEMENT, OUTCOME_GROUND_TRUTH
w = get_writer()                                          # 默认 JSONL + async，永不拖慢/中断主流程
sid = w.record_decision(trace_id, parent_span_id=None, service="geo-drill", run_id=rid,
        decision_type=DECISION_ORE_JUDGEMENT, state={...}, decision={...},
        rationale="...", rationale_source="rule", decision_maker="drill_judge")
w.record_outcome(trace_id, parent_span_id=sid, service="geo-drill", run_id=rid,
        outcome_type=OUTCOME_GROUND_TRUTH, ground_truth={...})
```

**broker 发现升级**：`find_*_for_bbox(..., trace_id=...)` 优先按 trace_id 精确匹配，未命中回退 bbox。

### 覆盖范围（全部 9 个 broker + 各服务 writer 已接入）

| 服务 | metadata writer | broker |
|---|---|---|
| geo-analyser | `alteration_store.save_batch_run`（manifest 1.2） | analyser_broker |
| geo-model3d | `outputs/writers.write_metadata` | model3d_broker |
| geo-drill | `outputs/writers.write_metadata`（+ D9 金标签） | drill_broker |
| geo-stru | `core/structural_engine.generate_maps` | structural_broker |
| geo-insar | `postprocess/deformation_evidence`（insar_metadata.json） | insar_broker |
| geo-geophys | `outputs/writers.write_metadata` | geophys_broker |
| geo-geochem | `outputs/writers.write_metadata` | geochem_broker |
| data-colle | `web_app._save_task_meta`（task_meta.json） | datacolle_broker |
| geo-exploration | `core/mineral_engine`（metadata.json） | exploration_broker |

> 各 writer 默认 self-generate trace_id；当 orchestrator(P2) 或前端显式传 `trace_id`、或 broker 命中上游时升级为 explicit/inherited。Harvester 的 `--harvest` 已覆盖全部 9 个 broker。

## 采集与导出（`harvest.py`）

```bash
python -m commons.trace.harvest --list                # 列出所有 trace 及闭环状态
python -m commons.trace.harvest --harvest             # 被动扫 broker，把产物补成 Outcome（零侵入覆盖未插桩服务）
python -m commons.trace.harvest --export OUT_DIR       # 导出训练样本
```

导出三类样本：
- `sft_decisions.jsonl` —— 决策点监督样本 `state → {decision, rationale}`
- `trajectories.jsonl` —— 端到端轨迹（含 `alternatives`，可做 DPO/RLHF 偏好学习）
- `grounded_pairs.jsonl` —— **闭环四元组**（model3d 预测上下文 × drill 实钻 ground-truth），**最高价值**

## 存储后端可换（`adapter.py`）

Writer 只产出带 `schema_version` 的纯 dict，交 `TraceAdapter` 持久化。换后端 = 换 Adapter + 一次批量 ETL，调用点零改动。

- 起步：`JSONLAdapter` → `${DEEPEXPLOR_TRACE_ROOT:-/opt/deepexplor-services/_traces}/<trace_id>.jsonl`
- 规模化：`PostgresAdapter`（JOIN/并发）
- 训练就绪：`ParquetAdapter`（列存/数据湖）

## 铁律

- **绝不影响业务**：所有 `record_*` / `stamp_metadata` 内部吞异常只记日志，trace 失败不报错。
- **绝不拖慢主流程**：默认 async，调用只入队，后台线程落盘；被动采集完全离线。
- **重产物只存路径**，trace 与产物解耦保留周期（产物可清，轨迹长留作训练资产）。
