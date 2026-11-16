# 设备保养采录台

离线导入工厂保养终端事件，以冻结的工单规程、仪表量程和校准期、领用零件以及签认事件判定保养是否完成。数据为合成示例，不联网。

Python 3.11+，`python -m pip install -e .` 安装。`python -m maintlog demo` 一条命令生成 SQLite 与 JSON/Markdown 履历；`python -m maintlog --help` 查看命令；`python -m pytest -q` 运行测试。

Source 保存原始字节和 SHA-256，Event 以终端与序号去重、保留行号；WorkOrder 绑定设备与规程版本，Step、Instrument、Stock 是规则输入，Issue 和 Decision 保存可审计结论。状态为待接收、已校验、待核、已判定、作废；事件状态机为 new→active↔paused→signed，void 终止。缺少前置步骤、失效仪表、越界读数、零件领用不符或非法事件不会被验收。结论分完成、需返工、证据不足、作废；报告引用原始事件 ID。

Run 记录每次重算状态；Evidence 逐事件关联来源与载荷哈希。导入和判定各在 SQLite 事务内提交，失败回滚。

## 步骤级时限验收

冻结计划可为工单选择计时契约（`work_order.contract`）并为步骤配置时限（`step.time_limit_minutes`）：`schedule` 班组排程按墙钟计时，暂停期间仍占用维修窗口；`piecework` 合同计件按有效作业时间计时，只有有证据（带原因且与其他事件不矛盾）的暂停区间不计入。步骤窗口从工单激活或上一步完成起、到本步完成为止，步骤间暂停归属于下一步；每段时长在结论中引用原始事件 ID，报告可解释超时发生在哪个步骤及具体差值。pause/resume 必须成对且不重叠；暂停缺少恢复、备件领用早于暂停、步骤在暂停期间签认、终端序号缺口或时钟倒退都会转为证据不足并阻断时限结算（不作零时长，也不选择更有利的时钟），作废工单不结算。更改时限配置后重新 `init` 再 `evaluate` 会产生新 Run 并重算，旧 Run 的决策保留其输入快照与规则版本；重复导入同一文件不会重复累计时长。
