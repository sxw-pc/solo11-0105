# 设备保养采录台

离线导入工厂保养终端事件，以冻结的工单规程、仪表量程和校准期、领用零件以及签认事件判定保养是否完成。数据为合成示例，不联网。

Python 3.11+，`python -m pip install -e .` 安装。`python -m maintlog demo` 一条命令生成 SQLite 与 JSON/Markdown 履历；`python -m maintlog --help` 查看命令；`python -m pytest -q` 运行测试。

Source 保存原始字节和 SHA-256，Event 以终端与序号去重、保留行号；WorkOrder 绑定设备与规程版本，Step、Instrument、Stock 是规则输入，Issue 和 Decision 保存可审计结论。状态为待接收、已校验、待核、已判定、作废；事件状态机为 new→active↔paused→signed，void 终止。缺少前置步骤、失效仪表、越界读数、零件领用不符或非法事件不会被验收。结论分完成、需返工、证据不足、作废；报告引用原始事件 ID。

Run 记录每次重算状态；Evidence 逐事件关联来源与载荷哈希。导入和判定各在 SQLite 事务内提交，失败回滚。

## 保修结算与补差

`register` 只冻结“完成”工单的技术决定、替换事件与来源哈希、不可改写的已发行发票（INV-001）和预授权保修合同，不计算任何应收或退款。`settle` 按合同把冻结事实转成可审计结算：

- 仅当技术结论为“完成”、替换件与领用及原事件身份一致、价格/合同/发票均有效时才可最终出账（已出账）；待核、返工、作废、零件身份冲突或来源异文时整张结算待核，不把未证成零件当免费、也不先收部分行。
- 劳务与零件按整数分计价；保修免赔额对整张工单覆盖行只扣一次。客户应付+承保方应付逐分等于含已验收项目的总价；多行尾数用纯整数最大余数规则（并列按行号稳定）分摊，无浮点误差。
- INV-001 的金额与身份永不覆写；差额只以客户补收/退款分录（`customer_adjustment`，负数为退款）和承保方应收（`insurer_receivable`）表达。
- 价格、合同或技术证据变化生成新 revision：先以 `reversal` 分录逐分冲回上一有效调整，再形成新净补差，绝不在原发票上重复累计；相同合同/证据快照重放幂等。所有金额调整、冲回与净余额可沿 `settlement_entry` 凭证重放且逐分守恒。

离线命令：

```
python -m maintlog register --db maintlog.db --case WARRANTY-1 --fixture fixtures/warranty-case.json
python -m maintlog settle --db maintlog.db --case WARRANTY-1 --contract parts_covered
python -m maintlog settle --db maintlog.db --case WARRANTY-1 --contract labor_covered
python -m maintlog settlement-report --db maintlog.db --out settlement.json
```

同一技术事实下 `parts_covered`（只保零件）产生客户补收 3000 分，`labor_covered`（只保劳务）产生客户退款 3000 分；两份合同是独立结算流，各自的真实债权/退款都保留，不挑有利口径、不合并。表：`settlement_stream`、`settlement_revision`、`settlement_line`、`settlement_entry`（对旧库以 `CREATE TABLE IF NOT EXISTS` 自动迁移）。
