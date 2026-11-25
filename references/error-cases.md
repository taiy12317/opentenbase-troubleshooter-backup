# OpenTenBase 故障知识库

诊断规则与错误签名的知识来源。每条目标注证据等级：

- **reproduced**：在本实验集群（OpenTenBase v5.0，PG10 基座，1 GTM + 1 CN + 2 DN）实测复现；
- **source-verified**：OpenTenBase 源码核验；
- **community-evidence**：来自社区 Issue/PR 记录，未经本环境复现；
- **tool-design**：工具自身的设计约定，非故障知识。

## 一、连接与启动类错误

| 错误签名 | 触发条件 | 常见原因 | 排查方法 | 来源 |
|---|---|---|---|---|
| `error while loading shared libraries: libreadline.so.7 / libssl.so.1.1 ... cannot open shared object file`（exit 127） | psql/postgres 启动即退 | LD_LIBRARY_PATH 未设置或不完整（缺 runtime/libpq、compat/lib、compat/usr/lib 目录） | `source <安装目录>/env.sh` 后重试；`ldd <pg_bin>/postgres` 核对缺失库 | **reproduced** |
| `Expected OpenTenBase libpq.so.5.10 symlink not found` | 部署校验（实验环境 start_lab.sh 首段） | runtime/libpq/libpq.so.5 未指向 pg/lib/libpq.so.5.10 | `ln -sf $(readlink -f <pg>/lib/libpq.so.5.10) <runtime>/libpq.so.5` | **reproduced** |
| `psql: could not connect to server: Connection refused`（exit 2） | 客户端到目标 host/port 被拒绝 | 端口无监听（节点未启动/已停/端口配置错误）；连接串 host/port 错误；网络、防火墙、隧道、安全组拦截 | 核对实际连接的 host/port；服务器侧 `ss -ltn` 确认监听；从实际客户端环境重试；`pg_ctl/gtm_ctl status` 核对节点状态。服务器本机 LISTEN 与客户端可达是两件独立事实，须分别确认 | **reproduced** |
| `FATAL: (28000) role "x" does not exist` | 连接时角色校验失败 | 连接用户不存在 | 使用已存在角色或 CREATE ROLE | **reproduced** |
| `FATAL: (3D000) database "x" does not exist` | 连接时目标库校验失败 | 目标库不存在 | createdb 或改用已存在库 | **reproduced** |
| `could not open configuration file ... Permission denied`（启动 FATAL） | 修改 postgresql.conf 后节点启动失败 | 以 root 修改配置文件后未恢复属主，数据库进程（opentenbase）无读取权限 | 将配置文件属主恢复为 opentenbase | **reproduced**（2026-11-24） |

## 二、SQL 执行期错误码（SQLSTATE）

| SQLSTATE | 含义 | 触发条件与常见原因 | 排查方法 | 来源 |
|---|---|---|---|---|
| 42601 | 语法错误 | SQL 语句语法不合法 | 核对语句语法 | reproduced |
| 42P01 | 表/视图不存在 | 对象不在当前 schema，或 search_path 未包含目标 schema | 检查 schema 与 search_path | reproduced |
| 22012 | 除零 | 表达式除数为 0 | 核对 SQL 表达式 | reproduced |
| 42883 | 函数不存在 | 函数名拼写错误、参数不匹配；OpenTenBase 无 `pg_current_setting()`，须用 `current_setting()` | 核对函数名与参数 | reproduced |
| 42501 | 权限不足 | 当前用户无所需权限；普通用户读不了 `pg_file_settings` | 配置检查需直接读配置文件（root 或属主身份） | reproduced |
| 42704 | 对象未定义（名称解析失败类别） | 同一错误码可由不同触发条件产生：①类型在 search_path 可见范围外（实测 `SET search_path=pg_catalog` 后 `'memories'::regtype` 报 `type "memories" does not exist`，而 public 中同名复合类型实际存在）；②OpenTenBase 特有文本 `PGXC Group x: group not defined`（节点组不存在）；③对象确实不存在（拼写/大小写/库不对） | 核对 `pg_type` + `pg_namespace`、`SHOW search_path`、原始 SQL | reproduced |
| 53300 | 连接数超限 | 连接数达到 max_connections 上限 | 检查连接水位，评估扩容或连接复用 | community-evidence |
| 40P01 | 死锁 | 事务间锁等待成环 | 分析锁等待关系 | community-evidence |
| 57P01 | admin shutdown | 三条路径：①管理员停节点或 `pg_terminate_backend` 终止会话 → 客户端收到 `FATAL: (57P01) terminating connection due to administrator command`，服务器日志同步 FATAL；②`CREATE/ALTER NODE` 未下发 FORWARD 选项 → `node_forward_port=0` → 分布式查询无法建立 Forward Node 通道而中止，客户端报 57P01（[Issue #194](https://github.com/OpenTenBase/OpenTenBase/issues/194)）；③分布式查询期间 DN 后端被终止 → DN 日志为 57P01 FATAL，CN 客户端报 08000（见 08000 行） | 结合服务器日志与节点状态区分路径 | ①reproduced；②community-evidence |
| 08000 | connection exception | 分布式查询期间 DN 后端被终止，CN 客户端报 `Failed to send plan`；DN 日志同步 57P01 FATAL，两侧同 coord 可关联 | 用 diagnose 日志关联确认两侧事件 | reproduced |
| 53000 | insufficient_resources | CN 无法从连接池获取 DN 连接（DN 停机/不可达/连接池耗尽）。实测（2026-11-24）：fast 停 DN1 后，CN 分布式查询报 53000 `Failed to get pooled connections: connect for dn0001(127.0.0.1:15401) failed ... Connection refused`，HINT 提示节点不可达；重启 DN1 后恢复。与 08000（DN 后端被杀）为不同故障路径 | 核对目标 DN 状态与可达性 | reproduced |
| XX000 | 内部错误 | 用户可触发的「不支持的功能」被 elog 误报为 XX000（应为 0A000 feature_not_supported）：`CREATE GLOBAL INDEX` 报 `not support global index yet`；复制表 `CREATE TRIGGER` 报 `TRIGGER is not supported on replication table in distributed mode`（[Issue #507](https://github.com/OpenTenBase/OpenTenBase/issues/507)，[PR #508](https://github.com/OpenTenBase/OpenTenBase/pull/508)、[PR #545](https://github.com/OpenTenBase/OpenTenBase/pull/545) 修复中） | 视为功能不支持类错误，避免按内部错误深挖 | reproduced（2026-11-25 实测两条触发语句） |

## 三、节点与网络类日志签名

| 日志行签名 | 含义与触发条件 | 来源 |
|---|---|---|
| `the database system is in recovery mode` | 节点处于崩溃恢复中，或被当主节点访问 | reproduced（cn.log 实测） |
| `default group not defined` | 节点组元数据缺失/初始化不完整 | reproduced（cn.log 实测） |
| `terminating connection due to administrator command` | 管理命令终止连接，或停节点清理；客户端收到时为 `FATAL: (57P01) ...` | reproduced（dn 日志实测 + 客户端 psql 实测） |
| `Failed to send plan` | 分布式查询期间 CN→DN 连接失效，执行计划无法下发；DN 侧日志常同步出现 57P01 FATAL，两侧同 coord 可关联 | reproduced（CN/DN 日志 coord(8307,3408) 配对） |
| `Failed to get pooled connections` | CN 无法从连接池获取 DN 连接：DN 停机/不可达/连接池耗尽；客户端报 53000 并附 HINT | reproduced（2026-11-24） |
| `Failed to read response from data node:dnXXXX pid:NNNN when ending query for ERROR` | CN 无法从 DN 读到查询结束响应（DN 侧连接失效）；配合 `node_forward_port=0` 出现时为 Issue #194 场景 | community-evidence（[Issue #194](https://github.com/OpenTenBase/OpenTenBase/issues/194)） |
| `received smart shutdown request` | 节点收到 smart 停机请求（pg_ctl stop 默认模式/平台回收脚本），随后连接被终止并干净关闭 | reproduced（2026-11-24 平台回收实测） |
| `database system was interrupted; last known up at ...` + `database system was not properly shut down; automatic recovery in progress` | 崩溃/立即停机后重启，自动恢复进行中；redo starts/done 记录恢复进度 | reproduced（2026-11-24 immediate 停 DN1 后重启实测） |
| `because tcp fails to send data, force kill SIGTERM to pid N` | 对端进程退出/网络中断，本端收不到确认 | reproduced（dn 日志实测） |
| `Changing connection state for node X pid: N, state A ==> B, invoker execRemote.c: 862` | CN 记录与 DN 连接的状态迁移；ERROR/FATAL 迁移说明 DN 侧出错 | reproduced（cn.log 实测） |
| `because got pgxc pool reload, force kill SIGTERM to pid N` | 连接池重载导致连接清理 | reproduced（dn 日志实测） |

## 四、跨节点日志关联机制

- 关联键与机制：默认 stderr 格式 `[pid,coord(coord_pid,coord_vxid)]`；DN 日志中的 coord 值由 CN 随每条语句下发（`'U'` 消息，postgres.c:8018-8041）。**source-verified + reproduced**。
- 关联判定：DN 侧 FATAL 与 CN 侧 LOG（连接状态迁移）同 coord 配对，确认两侧属于同一故障事件。
- 置信度分级：`confirmed`（双侧事件、时间窗口内）/ `candidate`（级别不足或疑似 PID 复用）/ `insufficient`（对侧无记录）。
- 全局 trace id 与 csvlog：开启 `logging_collector=on + log_destination='stderr,csvlog'`（postmaster 级参数，重启生效）后，trace id 写入 csv 的 user_name 字段（复用）；CN 侧格式 `NC<全局递增seq>_<vxid>_0_0_0_0`（seq 与 PGXCQueryId `cn0001_<seq>` 对应），DN 侧格式 `YB<pid>_0`；DN csv 无 PGXCQueryId/语句记录，trace id 无法做 CN↔DN 跨节点关联；stderr 日志与 csvlog 并存（log_destination 含 stderr 时 -l 文件照常写入）。工具以 stderr coord 关联为主，实测 csvlog 开启状态下 diagnose 正常。csvlog 默认关闭且开启需重启各节点，不作为默认依赖。**reproduced（2026-11-24）**。
- 关联判定需考虑：节点身份、时间窗口（默认 30s）、PID 复用（超窗口降级为候选）、日志缺失（标记证据不足）。

## 五、OpenTenBase 接口差异与特有行为

| 项 | 技术事实 | 来源 |
|---|---|---|
| `pg_stat_statements` | v5.0 实验环境不存在 | reproduced |
| `pgxc_lock_for_backends` | 该版本不存在 | reproduced |
| `EXECUTE DIRECT` | 仅超级用户可用 | reproduced |
| `pg_current_setting()` | 不存在，用 `current_setting()` | reproduced |
| `pg_stat_cluster_activity` | 存在，但含大量空 nodename 内部后端行，须过滤 `backend_type='client backend'` | reproduced |
| 配置重复定义 | 同参数多次出现时后者生效（实验环境 DN1 曾同时定义 max_connections=100 与 =80，生效 80） | reproduced |
| 普通表与复合类型 | 普通表创建时自动生成同名复合类型（`pg_type.typtype='c'`，`typrelid` 指回表）。实测 memories 表（relkind='r'）对应复合类型 typname='memories' 存在于 public，`NULL::memories` 可解析。`type "x" does not exist` 只说明当前 search_path 可见范围内没有该类型；排查时核对 pg_type + pg_namespace + SHOW search_path + 原始 SQL | reproduced |
| 日志位置 | logging_collector 默认 off，日志由启动脚本重定向（实验环境 /data/opentenbase-lab/logs/*.log），文件 600 属 opentenbase；GTM 为独立守护进程，其日志（gtm.log）非 PG stderr 格式，不能用 PG 日志解析器解析，默认日志扫描与跨节点关联只覆盖 CN/DN | reproduced |

## 六、数据分布与倾斜（inspect `data.skew`）

| 项 | 技术事实 | 来源 |
|---|---|---|
| 分布元数据 | `pgxc_class`：pclocatortype 'S'=分片（hash）、'R'=复制；分布列在 **`discolnums`**（int2vector，复制表为 NULL）——OpenTenBase v5 没有 Postgres-XL 的 `pcattnum` 列；`nodeoids` 列出承载节点 oid | reproduced（2026-11-25 实测：lab 中 memories/memory_history/memory_archive='S'，memory_concepts/memory_contradictions='R'） |
| 各 DN 行数 | `SELECT xc_node_id, count(*) FROM t GROUP BY xc_node_id` 可用；`xc_node_id` 对应 `pgxc_node.node_id`（v5 中 node_id 是大随机整数，非 1..n 递增） | reproduced（2026-11-25） |
| 判定口径 | 最大/最小行数比 ≥ 2 → WARN；完全倾斜（存在 0 行 DN）且总行数 ≥ 1 万（`--skew-fail-min-rows` 可调）→ FAIL，低于阈值降级 WARN。行数不均衡不影响查询正确性，影响并行查询性能与节点负载，也可能是有意为之（分布列取值集中） | reproduced（2026-11-25：2 万行均衡表 ratio 1.00；80/20 倾斜表 4.00 → WARN；1.5 万行全落单 DN → FAIL；5000 行全落单 DN → 降级 WARN） |
| 扫描安全 | `reltuples` 超过 `--skew-max-rows`（默认 10 万）或未 ANALYZE（-1）的表默认不做精确全扫；`--skew-tables`（schema.table 或裸表名）显式强制 | reproduced（2026-11-25） |
| 零扫描提示 | `pg_stats` 分布列 `n_distinct` ∈ (0,4) 或最常见值占比 ≥ 50% → 倾斜风险提示（仅提示，不参与状态判定）。`n_distinct=-1` 表示不同值数与行数相等；负值比例型仅 `|n_distinct|<0.1` 才提示 | reproduced（2026-11-25：k 列 n_distinct=2/最常见 80% → 提示；id 列 -1 → 不提示） |
| 调整分布 | 需重建表：`CREATE TABLE ... DISTRIBUTE BY SHARD(合适列)` 后迁移数据；只读巡检不自动调整 | tool-design |
