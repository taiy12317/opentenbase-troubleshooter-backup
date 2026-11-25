# OpenTenBase 错误案例库

诊断规则的知识来源。每条目标注证据等级：
- **reproduced**：在本实验集群（OpenTenBase v5.0，PG10 基座，1GTM+1CN+2DN）实测复现；
- **source-verified**：OpenTenBase 源码核验；
- **community-evidence**：来自社区 Issue/PR 记录，未经本环境复现。

禁止编造条目。诊断时优先引用本文件，证据不足则明确说明。

## 一、部署/环境类（exit 127 系列）

| 现象 | 原因 | 处理 |
|---|---|---|
| `error while loading shared libraries: libreadline.so.7: cannot open shared object file`（exit 127） | LD_LIBRARY_PATH 未设置或不完整（缺 runtime/libpq、compat/lib、compat/usr/lib 目录） | `source <安装目录>/env.sh` 后重试；`ldd <pg_bin>/postgres` 核对缺失库 | **reproduced** |
| `error while loading shared libraries: libssl.so.1.1 ...`（同上） | 同上 | 同上 | **reproduced** |
| `Expected OpenTenBase libpq.so.5.10 symlink not found` | runtime/libpq/libpq.so.5 未指向 pg/lib/libpq.so.5.10 | `ln -sf $(readlink -f <pg>/lib/libpq.so.5.10) <runtime>/libpq.so.5` | **reproduced**（实验环境 start_lab.sh 首段校验） |
| `psql: could not connect to server: Connection refused`（exit 2） | 客户端到目标 host/port 被拒绝：端口无监听（节点未启动/已停/端口错误）、连接串 host/port 错误、网络/防火墙/隧道问题。**服务器本机 LISTEN 不等于客户端可达**，须区分报错发生在客户端侧还是服务器侧 | 核对实际连接的 host/port；服务器侧 `ss -ltn`；从实际客户端环境重试；`pg_ctl/gtm_ctl status` 核对节点 | **reproduced**（连不存在端口实测） |
| `FATAL: (28000) role "x" does not exist`（exit 2） | 连接用户不存在 | 用已存在角色或 CREATE ROLE | **reproduced** |
| `FATAL: (3D000) database "x" does not exist`（exit 2） | 目标库不存在 | createdb 或改库名 | **reproduced** |

## 二、SQL 错误码（SQLSTATE）

| SQLSTATE | 含义 | OpenTenBase 备注 | 证据 |
|---|---|---|---|
| 42601 | 语法错误 | — | reproduced |
| 42P01 | 表/视图不存在 | 检查 schema 与 search_path | reproduced |
| 22012 | 除零 | — | reproduced |
| 42883 | 函数不存在 | **OpenTenBase 无 `pg_current_setting()`，须用 `current_setting()`** | reproduced |
| 42501 | 权限不足 | **普通用户读不了 `pg_file_settings`**，配置检查需直接读文件（root/属主） | reproduced |
| 42704 | 对象未定义（名称解析失败类别，**非单一根因**） | 本环境实测两类：①类型在 search_path 可见范围外——`SET search_path=pg_catalog` 后 `'memories'::regtype` 报 `type "memories" does not exist`，而 public 中同名复合类型实际存在；②OpenTenBase 特有文本 `PGXC Group x: group not defined`（节点组不存在）。其他可能：对象确实不存在（拼写/大小写/库不对） | reproduced |
| 28000 | 认证/角色失败 | — | reproduced |
| 3D000 | 数据库不存在 | — | reproduced |
| 53300 | 连接数超限 | — | community-evidence |
| 40P01 | 死锁 | — | community-evidence |
| 57P01 | admin shutdown | 不必然表示管理员主动停节点。本环境实测两条路径：①`pg_terminate_backend` 终止会话 → 客户端 psql 收到 `FATAL: (57P01) terminating connection due to administrator command`，服务器日志同步 FATAL；②分布式查询期间杀 DN 后端 → **客户端报 08000 `Failed to send plan`**，DN 日志为 57P01 FATAL（Issue #194 客户端表现） | reproduced |
| 08000 | connection exception | 本环境实测：分布式查询期间 DN 后端被终止，CN 客户端报 08000 `Failed to send plan`；DN 日志同步 57P01 FATAL，两侧同 coord 可关联 | reproduced |
| 53000 | insufficient_resources | 本环境实测（2026-11-24）：fast 停 DN1 后，CN 客户端分布式查询报 53000 `Failed to get pooled connections: connect for dn0001(127.0.0.1:15401) failed, errmsg: could not connect to server: Connection refused`，HINT 提示节点不可达；重启 DN1 后恢复。与 08000（DN 后端被杀）为不同故障路径 | reproduced |
| XX000 | 内部错误 | 2026-11-25 实测复现 Issue #507 两条触发语句：`CREATE GLOBAL INDEX` 报 `(XX000) not support global index yet`；复制表 `CREATE TRIGGER` 报 `(XX000) TRIGGER is not supported on replication table in distributed mode`。均为「不支持的功能」被 elog 误报为 XX000（应报 0A000 feature_not_supported）；PR #508/#545 修复中 | reproduced |

## 三、节点/网络类日志签名（跨节点关联素材）

| 日志行签名 | 含义 | 证据 |
|---|---|---|
| `the database system is in recovery mode` | 节点处于崩溃恢复中，或被当主节点访问 | reproduced（cn.log 实测） |
| `default group not defined` | 节点组元数据缺失/初始化不完整 | reproduced（cn.log 实测） |
| `terminating connection due to administrator command` | 管理命令终止连接，或停节点清理；常伴随 tcp 失败。客户端收到时为 `FATAL: (57P01) ...` | reproduced（dn 日志实测 + 2026-11-23 客户端 psql 实测） |
| `Failed to send plan` | 分布式查询期间 CN→DN 连接失效，执行计划无法下发；DN 侧日志常同步出现 57P01 FATAL，两侧同 coord 可关联 | reproduced（2026-11-23 实测，CN/DN 日志 coord(8307,3408) 配对） |
| `Failed to get pooled connections` | CN 无法从连接池获取 DN 连接：DN 停机/不可达/连接池耗尽。实测 fast 停 DN1 → 客户端 53000 + 该文本 + HINT 节点不可达；重启后恢复 | reproduced（2026-11-24 实测） |
| `received smart shutdown request` | 节点收到 smart 停机请求（pg_ctl stop 默认模式/平台回收脚本），随后连接被终止（terminating connection due to administrator command）并干净关闭 | reproduced（2026-11-24 平台回收实测，cn.log 完整序列） |
| `database system was interrupted; last known up at ...` + `database system was not properly shut down; automatic recovery in progress` | 崩溃/立即停机后重启，自动恢复进行中；redo starts/done 记录恢复进度 | reproduced（2026-11-24 实测：immediate 停 DN1 后重启捕获完整序列） |
| `because tcp fails to send data, force kill SIGTERM to pid N` | 对端进程退出/网络中断，本端收不到确认 | reproduced（dn 日志实测） |
| `Changing connection state for node X pid: N, state A ==> B, invoker execRemote.c: 862` | CN 记录与 DN 连接的状态迁移；ERROR/FATAL 迁移说明 DN 侧出错 | reproduced（cn.log 实测） |
| `because got pgxc pool reload, force kill SIGTERM to pid N` | 连接池重载导致连接清理 | reproduced（dn 日志实测） |

## 四、跨节点日志关联（已实测机制）

- 默认 stderr 格式 `[pid,coord(coord_pid,coord_vxid)]`：DN 上 coord 值由 CN 每条语句下发（`'U'` 消息，postgres.c:8018-8041）。**source-verified + reproduced**。
- 关联键 = `coord(coord_pid,coord_vxid)`；DN 侧 FATAL 与 CN 侧 LOG（连接状态迁移）可配对确认同一故障事件。
- 全局 trace id（NC 前缀）仅写入 csvlog（elog.c:3154-3156）。**2026-11-24 实测**：开启 `logging_collector=on + log_destination='stderr,csvlog'`（postmaster 级参数，重启生效）后，trace id 写在 csv 的 user_name 字段（复用）；CN 侧格式 `NC<全局递增seq>_<vxid>_0_0_0_0`（seq 与 PGXCQueryId `cn0001_<seq>` 对应），DN 侧格式 `YB<pid>_0`；DN csv 无 PGXCQueryId/语句记录，**trace id 无法做 CN↔DN 跨节点关联**；stderr 日志与 csvlog 并存（log_destination 含 stderr 时 -l 文件照常写入）。工具默认依赖 coord 关联，实测 csvlog 开启状态下 diagnose 正常。csvlog 默认关闭且开启需重启各节点，不可作为默认依赖。
- 关联必须考虑：节点身份、时间窗口（默认 30s）、PID 复用（超窗口降级为候选）、日志缺失（标记证据不足）。
- 改配置重启注意事项（2026-11-24 实测教训）：root 备份/还原 postgresql.conf 后**必须 chown 回 opentenbase**，否则启动报 `could not open configuration file ... Permission denied` + FATAL。

## 五、OpenTenBase 与 PostgreSQL 的接口差异（避免踩坑）

| 项 | 事实 | 证据 |
|---|---|---|
| `pg_stat_statements` | v5.0 实验环境不存在 | reproduced |
| `pgxc_lock_for_backends` | 该版本不存在 | reproduced |
| `EXECUTE DIRECT` | 仅超级用户可用 | reproduced |
| `pg_current_setting()` | 不存在，用 `current_setting()` | reproduced |
| `pg_stat_cluster_activity` | 存在，但含大量空 nodename 内部后端行，须过滤 `backend_type='client backend'` | reproduced |
| 配置重复定义 | 同参数多次出现时后者生效（实验环境 DN1 曾同时定义 max_connections=100 与 =80，生效 80） | reproduced |
| 普通表与复合类型 | 普通表创建时自动生成同名复合类型（`pg_type.typtype='c'`，`typrelid` 指回表）。实测 memories 表（relkind='r'）对应复合类型 typname='memories' 存在于 public，`NULL::memories` 可解析。**不能由 relkind='r' 推断「类型不存在」**；`type "x" does not exist` 只说明当前 search_path 可见范围内没有该类型 | reproduced |
| 日志位置 | logging_collector 默认 off，日志由启动脚本重定向（实验环境 /data/opentenbase-lab/logs/*.log），文件 600 属 opentenbase | reproduced |

## 六、数据分布与倾斜（inspect `data.skew`）

| 项 | 事实 | 证据 |
|---|---|---|
| 分布元数据 | `pgxc_class`：pclocatortype 'S'=分片（hash）、'R'=复制；分布列在 **`discolnums`**（int2vector，复制表为 NULL）——OpenTenBase v5 **没有** Postgres-XL 的 `pcattnum` 列；`nodeoids` 列出承载节点 oid | reproduced（2026-11-25 实测：lab 中 memories/memory_history/memory_archive='S'，memory_concepts/memory_contradictions='R'） |
| 各 DN 行数 | `SELECT xc_node_id, count(*) FROM t GROUP BY xc_node_id` 可用；`xc_node_id` 对应 `pgxc_node.node_id`（v5 中 node_id 是大随机整数，不是 1..n 递增） | reproduced（2026-11-25 实测） |
| 判定口径 | 最大/最小行数比 ≥ 2 → WARN；完全倾斜（存在 0 行 DN）且总行数 ≥ 1 万（`--skew-fail-min-rows` 可调）→ FAIL，低于阈值降级 WARN。**不均衡≠故障**：不影响正确性，影响并行查询性能与节点负载，也可能是有意为之（分布列取值集中） | reproduced（2026-11-25 实测：2 万行均衡表 ratio 1.00；80/20 倾斜表 4.00 → WARN；1.5 万行全落单 DN → FAIL；5000 行全落单 DN → 降级 WARN） |
| 扫描安全 | `reltuples` 超过 `--skew-max-rows`（默认 10 万）或未 ANALYZE（-1）的表默认不做精确全扫；`--skew-tables`（schema.table 或裸表名）显式强制 | reproduced（2026-11-25 实测阈值跳过与强制扫描路径） |
| 零扫描提示 | `pg_stats` 分布列 `n_distinct` ∈ (0,4) 或最常见值占比 ≥ 50% → 倾斜风险提示（**仅提示，不参与状态判定**）。**`n_distinct=-1` 表示不同值数与行数相等，不是风险**；负值比例型仅 `|n_distinct|<0.1` 才提示 | reproduced（2026-11-25 实测：k 列 n_distinct=2/最常见 80% → 提示；id 列 -1 → 不提示） |
| 调整分布 | 需重建表：`CREATE TABLE ... DISTRIBUTE BY SHARD(合适列)` 后迁移数据；只读巡检不自动调整 | otb_note |

## 七、报告规范

- 区分：已确认事实（facts）/ 可能原因（causes）/ 待验证假设（hypotheses）。
- 证据不足输出「无法确定根因」。
- 历史错误 ≠ 当前故障，引用日志必须带时间。
- 存在 prepared transaction、参数差异、非 idle 会话都不是必然异常。
- 退出码语义：preflight/inspect 为健康检查（0 无 FAIL/1 有 FAIL/2 执行不完整/3 参数错误）；diagnose 为离线分析（0 分析完成/2 执行不完整/3 参数错误），`--strict` 为可选检测模式（命中 FAIL/WARN 签名或 confirmed 关联返回 1，仅表示识别到异常，不等于当前故障）。
- 跨节点关联 confirmed 只说明两侧日志属于同一执行上下文（关联可靠性），不等于故障根因已确认；JSON 结果含 `root_cause` 字段显式区分。
- SQLSTATE 只标识错误类别，同一错误码可由不同原因触发；缺少原始 SQL 时不得断言具体根因。
