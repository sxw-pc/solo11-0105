# 设备保养采录台

离线导入工厂保养终端事件，以冻结的工单规程、仪表量程和校准期、领用零件以及签认事件判定保养是否完成。数据为合成示例，不联网。

Python 3.11+，`python -m pip install -e .` 安装。`python -m maintlog demo` 一条命令生成 SQLite 与 JSON/Markdown 履历；`python -m maintlog --help` 查看命令；`python -m pytest -q` 运行测试。

Source 保存原始字节和 SHA-256，Event 以终端与序号去重、保留行号；WorkOrder 绑定设备与规程版本，Step、Instrument、Stock 是规则输入，Issue 和 Decision 保存可审计结论。状态为待接收、已校验、待核、已判定、作废；事件状态机为 new→active↔paused→signed，void 终止。缺少前置步骤、失效仪表、越界读数、零件领用不符或非法事件不会被验收。结论分完成、需返工、证据不足、作废；报告引用原始事件 ID。

Run 记录每次重算状态；Evidence 逐事件关联来源与载荷哈希。导入和判定各在 SQLite 事务内提交，失败回滚。

## 采购下发

已验收换件事件与报价原文冻结为 `procurement_case` 后，可离线签发两种仓方授权合同：

- `min_total_cost`：先最小化完整采购总价，总价相同时最小化所有 SKU 的最大正逾期；
- `min_max_lateness`：先最小化最大正逾期，逾期相同时最小化总价。

```bash
python -m maintlog procure-register BUY-1 --db out/maintlog.db --case fixtures/procurement-case.json
python -m maintlog procure-plan BUY-1 min_total_cost --db out/maintlog.db
python -m maintlog procure-plan BUY-1 min_max_lateness --db out/maintlog.db
python -m maintlog procure-report --db out/maintlog.db --out out/procurement.json
```

两种合同都必须整单满足全部需求：整数数量、逐行报价容量、以及按“本次对该供应商的全部 SKU 采购额”联立计算的起订额；任何缺 SKU、报价异文、价格非正、容量不足、证据哈希不符或起订额不足都会阻止整张订单写库，不签发部分采购单。求解器枚举供应商子集并对每个子集精确求最小成本整数分配，最优性证据（含各逾期上限下的完整前沿）随订单冻结在 `procurement_plan.evidence`。

已签发订单为只追加记录：SQLite 触发器禁止改写订单与分行，重复规划同一输入返回原订单而不重复下单。新报价须用 `procure-register --allow-revision` 登记，修订会作废旧计划并另签新计划，报告中给出须取消的旧订单号与逐供应商/逐 SKU 差额，绝不把两份订单累加为同一需求。

`fixtures` 案例中 NEW-1 需 2 件、期限第 3 日，GASKET 需 1 件、期限第 6 日：最低成本合同向 S 下 10000 分整单（NEW-1 逾期 2 天），最短缺料合同向 F 下 16000 分整单（最大逾期 0 天）。把 NEW-1 买自 F、只把 GASKET 买自 S 的逐行方案不成立，因为 S 该单仅 2000 分，低于其 10000 分起订额。
