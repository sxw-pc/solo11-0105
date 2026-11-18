# 设备保养采录台

离线导入工厂保养终端事件，以冻结的工单规程、仪表量程和校准期、领用零件以及签认事件判定保养是否完成。数据为合成示例，不联网。

Python 3.11+，`python -m pip install -e .` 安装。`python -m maintlog demo` 一条命令生成 SQLite 与 JSON/Markdown 履历；`python -m maintlog --help` 查看命令；`python -m pytest -q` 运行测试。

Source 保存原始字节和 SHA-256，Event 以终端与序号去重、保留行号；WorkOrder 绑定设备与规程版本，Step、Instrument、Stock 是规则输入，Issue 和 Decision 保存可审计结论。状态为待接收、已校验、待核、已判定、作废；事件状态机为 new→active↔paused→signed，void 终止。缺少前置步骤、失效仪表、越界读数、零件领用不符或非法事件不会被验收。结论分完成、需返工、证据不足、作废；报告引用原始事件 ID。

Run 记录每次重算状态；Evidence 逐事件关联来源与载荷哈希。导入和判定各在 SQLite 事务内提交，失败回滚。

## 技师隐私与受众隔离发布

外部合作方只能取得精确群组合计；任何候选都要与该受众已持有（含占位预订）的全部群组报表做**联合线性推断检查**（`maintlog/inference.py`，分数精确 RREF）：若联立方程唯一决定任一技师的个人次数即拒绝，例如外部已持 R1={P1,P2,P3}=6 时，R2={P1,P2}=3 会推出 P3=6−3=3，故 R2 必须拒绝（拒绝行不保存被保护次数，仅存原因码与推断证据摘要）。两人群组、删除姓名或只检查最新一份都不构成充分保护。内部审计合同则可发布逐技师准确视图，逐事件保留 event/source/payload 哈希以便复核。

发布身份绑定受众、事件版本（逐事件哈希指纹）、来源摘要、真实成员集合、合同摘要与报表摘要；已被外部取得的 R1 镜像进持久账本，改报表名、换日期、重新导入或修正读数都不会被遗忘。每次发布走可恢复两阶段协议：先提交 `placeholder` 账本行，再 tmp+fsync+rename 落盘 JSON/Markdown，最后翻转为 `published`；`recover` 协调账本与文件（重生成校验、失败隔离、无账本孤儿文件隔离），不会出现文件可取而账本不知、或账本标记已公开却无文件。

CLI（离线，不联网）：

- `python -m maintlog register-privacy --db m.db --case-id PRIV-1 --fixture fixtures/technician-privacy-case.json` 冻结真实 operator 计数与既有 R1
- `python -m maintlog review --db m.db --fixture … --audience COMMUNITY-PARTNER|INTERNAL-AUDIT --out-dir rel [--contract …] [--report-id …]` 审查并按两阶段协议发布或拒绝
- `python -m maintlog materialize --db m.db --out-dir rel` 按受众目录重新物化已发布文件（外部文件不含 event.payload）
- `python -m maintlog recover --db m.db --out-dir rel [--audience …]` 崩溃后协调恢复
- `python -m maintlog ledger --db m.db [--audience …]` 查看发布/拒绝/中止账本

原 `report` 工单履历报告保持兼容，仍含完整 event.payload，仅供内部使用。
