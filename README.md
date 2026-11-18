# 设备保养采录台

离线导入工厂保养终端事件，以冻结的工单规程、仪表量程和校准期、领用零件以及签认事件判定保养是否完成。数据为合成示例，不联网。

Python 3.11+，`python -m pip install -e .` 安装。`python -m maintlog demo` 一条命令生成 SQLite 与 JSON/Markdown 履历；`python -m maintlog --help` 查看命令；`python -m pytest -q` 运行测试。

Source 保存原始字节和 SHA-256，Event 以终端与序号去重、保留行号；WorkOrder 绑定设备与规程版本，Step、Instrument、Stock 是规则输入，Issue 和 Decision 保存可审计结论。状态为待接收、已校验、待核、已判定、作废；事件状态机为 new→active↔paused→signed，void 终止。缺少前置步骤、失效仪表、越界读数、零件领用不符或非法事件不会被验收。结论分完成、需返工、证据不足、作废；报告引用原始事件 ID。

Run 记录每次重算状态；Evidence 逐事件关联来源与载荷哈希。导入和判定各在 SQLite 事务内提交，失败回滚。

## 保养后废液物料流

`waste_disposition` 在不改写工单结论的前提下，把完成检修后封存清洗废液的实物去向做成可复核的逐容器物料账（全部质量以 `Decimal` 精确到 0.001kg）：

- `waste-register` 冻结完成工单、封条容器称重/污染物 CSV（含原文 SHA-256）、检查批次与两条经环境负责人授权的路径合同；重复导入/重跑幂等，不重复造料。
- `closed_loop_sep`：证书有效时在单个事务内原子消耗父容器，生成净液（污染物恒为 0，可回用）与浓缩残渣（危废）子批及分离复验凭证；质量与污染物逐项守恒。
- `sealed_ship`：保持原封父容器身份，按持证承运方联单与签收质量追踪完整/部分/拒签，未签收质量留在真实 `待运` 保管；承运方回退只追加事件并转入 `待核`，绝不悄悄改回现场库存。
- 缺称重、封条异文、负质量、污染物超总量、资质过期或父批已移交时，相关批次进入 `待核` 并留不可变挂起事件；同一容器不能既被分离回用又作为原封危废外运。
- `waste-report` 输出 JSON 物料账与联单报告，含逐容器事件增量与账面余额的守恒校验；`waste-retest` 追加后补复验凭证（旧实物事件保留）。

命令见 `python -m maintlog --help`（`waste-register`、`waste-dispose`、`waste-sign`、`waste-retest`、`waste-return`、`waste-report`）。

