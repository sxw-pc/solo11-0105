# 设备保养采录台

离线导入工厂保养终端事件，以冻结的工单规程、仪表量程和校准期、领用零件以及签认事件判定保养是否完成。数据为合成示例，不联网。

Python 3.11+，`python -m pip install -e .` 安装。`python -m maintlog demo` 一条命令生成 SQLite 与 JSON/Markdown 履历；`python -m maintlog --help` 查看命令；`python -m pytest -q` 运行测试。

Source 保存原始字节和 SHA-256，Event 以终端与序号去重、保留行号；WorkOrder 绑定设备与规程版本，Step、Instrument、Stock 是规则输入，Issue 和 Decision 保存可审计结论。状态为待接收、已校验、待核、已判定、作废；事件状态机为 new→active↔paused→signed，void 终止。缺少前置步骤、失效仪表、越界读数、零件领用不符或非法事件不会被验收。结论分完成、需返工、证据不足、作废；报告引用原始事件 ID。

Run 记录每次重算状态；Evidence 逐事件关联来源与载荷哈希。导入和判定各在 SQLite 事务内提交，失败回滚。

## 隔离许可

登记了 permit_case 的工单（带电设备检修）必须先取得隔离许可，终端 `start` 才能激活工单。`python -m maintlog register --db D --case C --fixture F` 冻结待接收工单、能量拓扑、上锁/解锁记录与双角色签认；`python -m maintlog permit --db D --case C --contract equivalent_cutset|all_local_points --at T` 在候选开工时刻重放有顺序、身份和签认的上锁记录，按 SITE-SAFETY 授权的合同裁决：`equivalent_cutset` 要求已核验锁切断每条能量源到设备的路径，`all_local_points` 要求拓扑标注的每个本地支路点各有有效锁。缺边、环、未知供能点不默认断电；缺签认、解锁后重锁未经复核、封条异文、起止时钟冲突都阻断许可，裁决给出能量路径或未锁点的具体证据。

许可绑定工单、锁事件/签名、拓扑摘要、合同、有效时段与一次性 fence；`ingest` 在记录 `start` 的同一事务内重核当前许可与锁状态，未获许可的 `start` 只作被阻断尝试留证（start_attempt + permit_denied），后续步骤不能借它进入 active。`revoke` 撤销许可；图或锁变化、迟到开工、重复导入都不能消费旧 token。`python -m maintlog permits --db D` 查看许可与阻断记录；报告输出隔离证据。
