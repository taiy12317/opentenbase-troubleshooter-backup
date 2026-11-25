# Codex 审查问题复核

日期：2026-11-23。复核对象：Codex 独立审查提出的 5 个潜在问题 + SQLSTATE 规则核对。
验证层级区分：**代码审查**（读源码）、**单元测试**（本地 pytest）、**Mock 反例**（构造假集群）、**真实 ECS 实验**（实验集群只读验证）、**Agent 实际调用**（Qoder Skill 端到端）。

---

## 逐问题复核

### 问题 1：跨节点日志关联可能包含查询时间窗口外的事件 —— 确认存在，已修复

| 项 | 内容 |
|---|---|
| 是否确实存在 | 是。Mock 反例复现：CN 窗口内 + DN 同 coord 键 30 小时前 FATAL，修复前该窗口外事件出现在关联结果的 dn_entries 中，且整个分组被按组内最大时间判为「窗口内」 |
| 源码位置 | `src/otb_tools/log_parser.py` correlate()；两处调用点 `diagnostics.py` diagnose_logs()、`inspector.py` _append_log_checks() |
| 根因 | 查询时间窗口过滤在关联分组之后、按组做（组内最大时间）；分组函数自身只有配对窗口（30s）概念 |
| 修复 | 两处调用点均在 correlate 前按 cutoff 预过滤输入事件（两个时间约束分层：查询窗口由调用方过滤，配对窗口由 correlate 的 window 参数控制）；corr_in_window 保留为兜底；insufficient 原因补充「或对侧事件在请求窗口外」 |
| 修复后测试 | 新增 6 项：DN 出窗被排除、CN 出窗被排除、同键混入窗口外事件只保留窗口内、窗口内两侧间隔 <30s 仍 confirmed（不因修复过度限制）、边界事件计入/排除、inspect 调用点同样过滤。全部通过 |
| 对现有功能影响 | 无接口变化；原有 fixture 确认关联（coord 2533,76）不变；配对窗口 30s 语义保留 |

### 问题 2：连接串密码脱敏 —— 部分确认，已修复

| 项 | 内容 |
|---|---|
| 是否确实存在 | `redact_conninfo` 本身对 8 类格式全部正确（单元测试锁定，无泄漏）。真实缺陷在两处：(a) `_conninfo_for_port` 正则重建连接串：`password='secret with spaces'` 被截断为 `password='secret` 且其余参数丢失，URI 百分号编码密码（`p%40ss`）不解码；(b) preflight `_run` 超时消息回显完整命令行（含连接串密码），db.probe 的 probe_env 明文显示 PGPASSWORD |
| 源码位置 | `src/otb_tools/inspector.py` _conninfo_for_port；`src/otb_tools/preflight.py` _run、_check_db_probe |
| 修复 | 改用 psycopg2 `make_dsn` 解析并重建连接串（正确处理引号/空格与百分号编码，host/port 覆盖）；超时消息不回显命令；probe_env 中 PGPASSWORD/PASSWORD 显示为 `***` |
| 验证记录 | psycopg2 2.9.13 异常消息不回显 DSN 密码（实测确认，无需修复）；未记录/打印/保存任何真实凭据，测试全用虚构密码 |
| 修复后测试 | 新增 14 项：quoted/URI 编码密码重建后 parse_dsn 值与端口覆盖正确、空 host 降级、超时消息不含命令行、db.probe 证据无密码、redact_conninfo 8 格式参数化。全部通过 |
| 对现有功能影响 | 无接口变化 |

### 问题 3：inspect 直连 DN 疑似只改端口、未用注册 host —— 确认存在，已修复

| 项 | 内容 |
|---|---|
| 是否确实存在 | 是。`pgxc_node.node_host` 读出但未使用，DN 直连与生效配置采集均沿用 CN 的 host。实验集群为单机（全部 127.0.0.1）故此前未暴露。Mock 反例（CN 192.0.2.10 / DN1 192.0.2.11 / DN2 192.0.2.12）复现：修复前所有 DN 连接均打到 192.0.2.10 |
| 源码位置 | `src/otb_tools/inspector.py` _conninfo_for_port、conn.direct_dn 循环、cfg.effective targets |
| 修复 | 新增 `_conninfo_for_node(conninfo, host, port)`；直连与生效配置均使用注册 host/port，其余参数（user/password/dbname/sslmode）保留；注册 host 缺失降级沿用原 host 并注明；端口缺失/非法时报 FAIL 证据并继续（顺带修复 `int(None)` 崩溃）；直连连接 try/finally 关闭；不可达 DN 如实报「直连失败」，不猜测 |
| 修复后测试 | Mock 多主机集群 3 项：直连与生效配置连接串 host/port 逐条断言、不可达 DN 如实 FAIL 且可达 DN 不受影响、缺端口节点不崩溃。全部通过 |
| 对现有功能影响 | 无接口变化；单机环境行为不变 |

### 问题 4：cfg.effective 的 ERROR 可能被后续 WARN 覆盖 —— 确认存在，已修复

| 项 | 内容 |
|---|---|
| 是否确实存在 | 是。参数差异循环无条件 `status = WARN`，采集失败（ERROR）被覆盖。Mock 反例：CN 成功(100) / DN1 连接失败 / DN2 成功(80)，修复前该项报 WARN、退出码 0，采集失败事实被掩盖 |
| 源码位置 | `src/otb_tools/inspector.py` cfg.effective 状态聚合段 |
| 修复 | 状态优先级 ERROR > WARN > PASS；采集失败与参数差异分开记录——facts 注明「仅 x/y 个节点采集成功，结论不完整」；「所有节点采集失败」与「部分采集失败」分 summary 表述 |
| 修复后测试 | 3 项：部分失败 + 存在差异 → ERROR + 退出码 2 + 「仅 2/3」；全部采集成功的差异 → WARN + 退出码 0；单节点查询异常 → 直连 FAIL 且该连接被关闭。全部通过 |
| 对现有功能影响 | 退出码语义未变（ERROR=2 优先级本就有），仅该项不再被误判 |

### 问题 5：失败查询毒化后续查询（25P02）与连接泄漏 —— 无法复现为缺陷，附带修复一处连接泄漏

| 项 | 内容 |
|---|---|
| 是否确实存在 | 事务毒化：**不存在**。`db.connect()` 已设置 `autocommit=True`，每条检查语句独立事务；run_sql 使用 `with conn.cursor()` 自动释放游标。真实 ECS 实验：同一连接上失败 SELECT（表不存在）后 SELECT 1 成功（无 25P02）；只读会话生效（CREATE TABLE 被 read-only 拒绝，探测未产生任何写入） |
| 附带缺陷 | 是。`conn.direct_dn` 的 DN 连接在 run_sql 抛异常时未关闭 → try/finally 修复 |
| 源码位置 | `src/otb_tools/db.py` connect()/run_sql()（无改动，验证其正确）；`inspector.py` direct_dn 循环（finally 关闭） |
| 修复后测试 | 2 项：connect 强制 autocommit 的白盒不变式（假 psycopg2 模块捕获）、run_sql 保留原始错误文本不吞异常。全部通过 |
| 对现有功能影响 | 无接口变化 |

---

## SQLSTATE 规则核对

| SQLSTATE | 结论 | 处理 |
|---|---|---|
| 57P01 | 原条目已含多因（Issue #194：分布式查询期间 DN 不可用也可表现 57P01），本轮拆分为两条独立原因。**2026-11-23 单机实测复现**（只终止本工具自建会话，不动节点）：`pg_terminate_backend` 后 psql 客户端收到 `FATAL: (57P01) terminating connection due to administrator command`，服务器日志同步 FATAL | 升级为 reproduced（diagnose 置信度 confirmed）；next 增加「客户端 08000 时联合查 DN 日志 57P01 FATAL」 |
| 08000 | 本轮新增条目：分布式查询期间杀自身会话的 DN 后端，CN 客户端实测报 `08000 Failed to send plan`，DN 日志同步 57P01 FATAL，两侧同 coord(8307,3408) 配对 | rules.py 新增 SQLSTATE 08000 + `failed-to-send-plan` 签名（reproduced）；真实日志 2h 窗口诊断中该事件关联为 confirmed |
| XX000 | 补充 Issue #507：部分「不支持的功能」被误报为 XX000（应为 0A000），「XX000 不必然是内部故障」。负向核对：本环境日志无 XX000 记录，10 项只读探测（多列 DISTINCT、逻辑复制视图、aggregate 等）均未触发 XX000 | 保持 community-evidence（candidate），负向核对结果写入 otb_note |

未扩大知识库规模（08000 条目来自本轮真实复现，非扩充猜测）；diagnose 结论模板固有「SQLSTATE 标识错误类别，不是单一根因」。

---

## 回归测试与验证汇总

| 类别 | 结果 |
|---|---|
| 原有测试 | 86 项全部通过（未删除、未修改预期） |
| 本轮新增测试 | `tests/test_codex_regressions.py` 33 项，全部通过（问题1×6、问题2×14、问题3×3、问题4×3、问题5×2、SQLSTATE×5）；本地合计 119 通过 |
| 真实 ECS 回归 | 18 项全部通过（只读，非破坏）：diagnose 72h 窗口原有确认关联 2533,76 仍成立且关联/错误明细无窗口外事件；inspect 退出码 0、无 ERROR、DN 直连与生效配置正常；连接不可达退出码 2、其余检查继续；失败查询后同连接继续成功 + 只读会话生效；preflight 探测成功；全部输出无虚构密码泄漏。新代码上传到独立目录 `/data/opentenbase-lab/tools/otb-codex-review/`，未覆盖现有部署副本 |
| 单机补充验证 | ①57P01 复现：终止自建会话 → psql 客户端捕获 `FATAL: (57P01)`，服务器日志同步 FATAL；②DN 后端被杀（Issue #194 场景）→ 客户端 08000 Failed to send plan，DN 日志 57P01 FATAL，真实日志 2h 窗口诊断中 coord(8307,3408) 判 confirmed；③host 覆盖真实验证：`_conninfo_for_node` 指定 127.0.0.2 时实际拨号 127.0.0.2（refused，因 DN 只绑 127.0.0.1），不可解析主机名按预期失败——证明 host 参数真实生效、不会静默连回 CN host；④XX000 负向核对：日志无记录 + 10 项只读探测未触发 |
| Agent Skill 回归 | 两 SKILL.md 仓库版与 `~/.qoder-cn/skills/` 注册副本逐字节一致；本轮未改 CLI 接口、退出码语义、诊断输出字段结构（规则内容更新不改结构），SKILL.md 无需更新，注册副本未动；references/error-cases.md 已同步 57P01/08000/XX000 证据等级 |
| 破坏性场景与 csvlog 实测（2026-11-24，用户授权） | ①fast 停 DN1：inspect 直连 DN1 如实 FAIL、DN2 不受影响、cfg.effective 部分失败 ERROR、exit 2；preflight --expect-running FAIL、exit 1；CN 查询 53000 Failed to get pooled connections（新案例入库，176 项测试全绿）；重启恢复。②immediate 停 DN1：捕获完整崩溃恢复日志（was interrupted/not properly shut down/redo done）。③csvlog：开启后 trace id 实测（NC/YB 前缀、user_name 字段、跨节点不可用）、diagnose 在 csvlog 开启下正常；配置已还原、集群健康（SELECT 1/0 残留 2PC/mem_server db_ok） |
| XX000 实测（2026-11-25） | 平台重启主机→重启集群后，实测复现 Issue #507 两条触发语句：`CREATE GLOBAL INDEX` 报 `(XX000) not support global index yet`、复制表 `CREATE TRIGGER` 报 `(XX000) TRIGGER is not supported on replication table in distributed mode`（临时表已清理）。XX000 条目升级 reproduced/confirmed，新增 `xx000-not-supported` 签名，177 项测试全绿；ECS 上 diagnose --sqlstate XX000 / --text 端到端验证通过。原"仍存问题 1（XX000 社区证据级别）"已解决移除 |

---

## 仍存在的关键问题

1. 日志关联依赖默认 stderr 格式的 `coord(pid,vxid)` 字段（不依赖 csvlog/trace id），格式变更时关联失效——已有文档说明。2026-11-24 补实测：csvlog 开启后 trace id 写入 csv user_name 字段（CN `NC<seq>_<vxid>_0_0_0_0` / DN `YB<pid>_0`），DN 侧无查询记录，**trace id 无法跨节点关联**，coord 是唯一实测可行的默认关联键。
2. 配对窗口固定 30s：同键两侧事件间隔超过 30s 会降级为 candidate（保守方向，不误报）。

## 定稿判断

5 个问题中 3 个确认为真实缺陷（关联窗口过滤、DN host、状态覆盖）、1 个部分确认（脱敏本身正确，连接串重建与超时回显为真实缺陷）、1 个无法复现（autocommit 设计已正确，附带修复连接泄漏），全部已修复并有针对性回归测试覆盖。原有 86 项测试全绿，新增 33 项全绿，ECS 真实环境 18 项只读验证全过；57P01 管理员终止路径已在单机真实复现并升级为 reproduced，新增 08000/Failed to send plan 真实案例入知识库，host 覆盖完成真实网络验证，Skill 无接口变化无需同步。**项目可作为课程实践任务定稿。**
