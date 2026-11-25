---
name: otb-cluster-inspector
description: OpenTenBase 集群运行状态巡检与故障诊断。当用户要求检查集群健康/运行状态、节点（GTM/CN/DN）状态、注册一致性、连接异常、活动会话、残留 2PC、数据分布与倾斜，或提供 SQLSTATE/错误文本要求排查原因，或要求分析 CN/DN 日志关联时使用。调用 otb-tools inspect（只读巡检：pgxc_node/pgxc_group/端口/进程/直连 DN/配置一致性/会话水位/2PC/数据倾斜 data.skew/日志扫描与 coord(pid,vxid) 跨节点关联）和 otb-tools diagnose（SQLSTATE、错误文本、日志证据诊断）。
---

# OpenTenBase 集群巡检与故障诊断（otb-cluster-inspector）

## 用途

运行期只读巡检 + 基于证据的故障诊断。`inspect` 输出结构化巡检报告；`diagnose` 对 SQLSTATE、错误文本或日志文件做签名匹配与跨节点关联。

职责边界：本 Skill 处理**集群已部署/运行中**的状态巡检与故障诊断。部署前环境检查、装不起来、启动失败、动态库缺失（exit 127）走 otb-deploy-preflight。

## 前置条件

CLI 位于 OpenTenBase Agentic Troubleshooting Toolkit 仓库（`src/otb_tools/`）。数据库查询需 psycopg2（`pip install 'otb-troubleshooting-toolkit[db]'`）；日志扫描与 diagnose 不需要。连接凭据一律通过 `--conn` 显式提供。

```bash
cd <toolkit 仓库根目录>
PYTHONPATH=src python3 -m otb_tools.cli inspect --conn "host=127.0.0.1 port=15001 dbname=lab user=<用户> password=<密码>" --format json
PYTHONPATH=src python3 -m otb_tools.cli diagnose --sqlstate 42704
```

实验 ECS 上的执行方式（workbench 为课程环境专用，其他环境忽略本段）：

```bash
workbench exec -i <实例ID> -r cn-beijing --timeout 180 -c \
  'cd /data/opentenbase-lab/tools/otb-troubleshooting-toolkit && PYTHONPATH=src python3 -m otb_tools.cli inspect --conn "host=127.0.0.1 port=15001 dbname=lab user=student" --format json'
```

## 调用时机

- 「检查集群运行状态」「巡检」「健康检查」；
- 节点异常：某节点连不上、注册信息可疑、连接数告警、2PC 残留；
- 数据分布疑问：某表是否倾斜、各 DN 数据量是否均衡；
- 用户贴出 SQLSTATE（如 42704/57P01/28000）或错误文本要求排查；
- 要求看 CN 和 DN 日志里有没有关联的异常事件。

## inspect 用法

```bash
otb-tools inspect --conn "<libpq 连接串>" [--log-dir /data/opentenbase-lab/logs] \
  [--log-files "cn=cn.log,dn1=dn1.log,dn2=dn2.log"] \
  [--log-window-hours 24] [--ports 15001,15401] \
  [--skew-max-rows 100000] [--skew-fail-min-rows 10000] [--skew-tables "schema.t,表名"] \
  [--watermark-threshold 0.8] [--timeout 5] [--output report.json] \
  [--strict] [--format json|text|markdown]
```

检查项：CN 连接、pgxc_node 注册、pgxc_group、注册端口 vs 实际监听、节点进程、CN→DN 直连、跨节点生效配置对比、会话与异常会话、连接水位、pg_prepared_xacts、数据分布与倾斜检查（data.skew）、日志错误扫描、跨节点日志关联（结果在 `correlations` 字段）。数据库连不上时自动降级：端口（需 `--ports`）、进程、日志检查继续执行，其余标记 SKIP。

data.skew 解读：按 `xc_node_id` 统计各 DN 行数——最大/最小 ≥ 2 → WARN（行数不均衡影响并行查询性能与节点负载，不影响查询正确性，也可能是有意为之）；完全倾斜（存在 0 行节点）且总行数 ≥ `--skew-fail-min-rows`（默认 1 万）→ FAIL，低于阈值降级 WARN。`reltuples` 超 `--skew-max-rows` 或未 ANALYZE 的表默认不精确扫描，`pg_stats` 零扫描风险提示仅作提示、不参与判定；`--skew-tables` 显式强制。调整分布需重建表（DISTRIBUTE BY SHARD），只读巡检不自动调整。

## diagnose 用法

```bash
otb-tools diagnose --sqlstate 42704
otb-tools diagnose --text "psql: error while loading shared libraries: libreadline.so.7: cannot open shared object file"
otb-tools diagnose --cn-logs /data/opentenbase-lab/logs/cn.log --dn-logs /data/opentenbase-lab/logs/dn1.log,/data/opentenbase-lab/logs/dn2.log --window-hours 24
otb-tools diagnose --log-files "cn=cn.log,dn1=dn1.log"          # 显式标角色，参与关联
otb-tools diagnose --log-files "/var/log/x.log"                  # 裸路径：只扫描错误，不参与关联
```

diagnose 退出码（离线分析语义，与 inspect 的健康检查语义不同）：0 = 分析完成；1 = `--strict` 检测模式且命中 FAIL/WARN 签名或 confirmed 关联（供脚本联动）；2 = 证据不完整（输入读取失败或日志全部行无法解析）；3 = 参数错误。

文本诊断分层匹配：完整错误文本命中具体签名（confirmed）；短症状文本（如裸 `Connection refused`）命中通用类别签名（candidate，只识别类别）；无命中时输出未匹配说明，继续补充 SQLSTATE 或节点日志。

## 诊断工作流

按用户报告选择入口，并主动用工具补充证据：

1. 错误文本 / SQLSTATE → `diagnose --text` / `--sqlstate`；涉及对象存在性（如 42704）时用 DB 查询核对（pg_type / pg_namespace / SHOW search_path / 原始 SQL）。
2. 连接失败 → 按「连接失败排查清单」收集证据。
3. 集群状态 → `inspect --format json`；节点异常时结合 `--ports` 与 `--log-files`。
4. 日志关联 → `diagnose --cn-logs … --dn-logs …`。

输出组织：异常现象（用户报告）→ 相关证据（工具 evidence/facts，引用日志时带时间）→ 可能原因（possible_causes）→ 后续排查建议（fix_hint/next_steps，或工具可继续补充的证据）。工具未确认的内容标为假设（hypotheses）并说明验证方法；证据不足时说明缺什么证据、如何获取（完整错误文本、SQLSTATE、原始 SQL、节点日志）。

## 连接失败排查清单（Connection refused / 连不上）

1. 用户实际连接的 host/port 与连接串（可能与本机部署不一致）；
2. 报错发生在客户端侧还是服务器侧（错误文本前缀、出现位置）；
3. 目标地址端口是否监听（服务器侧 `ss -ltn`，preflight/inspect 端口项）；
4. 从实际客户端环境能否连接：服务器本机 LISTEN 与客户端可达是两回事，容器、SSH 隧道、防火墙、云安全组都可能拦截；
5. 相关节点日志（启动日志、CN/DN 日志窗口内错误）；
6. 只有服务器侧状态、缺少用户实际连接信息时，把结论保留为假设（如「端口配置不一致（假设），需核对用户连接串」）。

## 结果解读

- 状态含义：PASS 正常；WARN 需关注但不判故障（参数差异、存在 2PC、非 idle 会话、窗口内历史错误）；FAIL 明确异常；ERROR 工具执行失败/证据不完整；SKIP 条件不满足未检查。
- inspect 退出码（健康检查语义）：0 无 FAIL 级问题（WARN 允许）/ 1 存在 FAIL（--strict 下 WARN 也计 1）/ 2 执行失败或关键检查不完整 / 3 参数错误。JSON 输出含 `execution_status`（工具是否执行完成）与 `health_status`（集群是否健康），两者含义不同。
- 关联置信度：`confirmed`（双侧事件、时间窗口内）/ `candidate`（级别不足或疑似 PID 复用）/ `insufficient`（对侧无记录）。置信度表示两侧日志是否属于同一执行上下文；根因判定由错误签名匹配给出（`root_cause` 字段）。
- SQLSTATE 只标识错误类别，同一错误码可由不同原因触发。42704（undefined_object）是名称解析失败类别：`type "x" does not exist` 指当前 search_path 可见范围内没有该类型——普通表会自动创建同名复合类型（OpenTenBase v5.0/PG10 实测），由 relkind='r' 不能推断「类型不存在」；核对 pg_type + pg_namespace + SHOW search_path + 原始 SQL 后下结论。
- 引用日志错误时带发生时间与窗口；参数差异、存在 prepared transaction、非 idle 会话均需结合判断依据报告。

## 约束

- 全部只读：不启停节点、不改配置、不清理 2PC、不杀会话。
- 节点连接失败时其他检查继续；日志不可读（权限不足）只影响日志项。
- 日志关联基于默认 stderr 格式的 `coord(pid,vxid)` 字段（DN 日志携带 CN 后端 pid，已实测）；不依赖 csvlog/global trace id（默认关闭）。
- 故障知识库与 OpenTenBase 特有接口限制（无 pg_stat_statements、无 pgxc_lock_for_backends、无 pg_current_setting、普通用户读不了 pg_file_settings）见仓库 `references/error-cases.md`。
