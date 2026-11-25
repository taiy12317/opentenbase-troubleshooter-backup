# OpenTenBase Agentic Troubleshooting Toolkit

面向 OpenTenBase v5.0（Postgres-XL 架构）的只读运维排障工具集：一个共享 Python CLI（`otb-tools`，三个子命令）+ 两个 Qoder Agent Skill。设计目标不是替代内核，而是把散落在 issue、日志、手工排查中的经验固化成可执行、可验证、Agent 可调用的工具。

## 组成

| 部件 | 位置 | 说明 |
|---|---|---|
| CLI `otb-tools` | `src/otb_tools/` | `preflight` / `inspect` / `diagnose` 三个子命令，共享结果模型与诊断规则 |
| Skill：otb-deploy-preflight | `skills/otb-deploy-preflight/` | 部署环境检查与安装/启动故障排查（Agent 调用入口） |
| Skill：otb-cluster-inspector | `skills/otb-cluster-inspector/` | 集群运行状态巡检与故障诊断（Agent 调用入口） |
| 错误案例库 | `references/error-cases.md` | 签名/SQLSTATE/跨节点关联机制，全部来自真实复现或 issue 实证 |
| 独立预检工具 | `contrib-standalone/otb_preflight.py` | 单文件无第三方依赖版本，作为向 OpenTenBase 贡献的最小候选物 |

## 支持范围（与不支持）

**支持**：

- `preflight`：部署前/启动失败后的环境只读检查——二进制、LD_LIBRARY_PATH、动态库（ldd）、libpq 符号链接、端口、目录、磁盘、配置文件重复参数（后者生效）、pg_hba trust、psql 数据库探测。默认按部署前场景（不要求数据库运行）；提供 `--conn` 时追加部署后验证（仅连接探测，运行期巡检由 `inspect` 负责）。已知失败签名匹配（libreadline/libssl/libpq 缺失 → exit 127、Connection refused、28000/3D000 等）。
- `inspect`：运行期只读巡检——CN 连接、pgxc_node 注册、pgxc_group、注册端口 vs 实际监听、节点进程、CN→DN 直连、跨节点生效参数对比、会话与连接水位、pg_prepared_xacts、数据分布与倾斜检查（各 DN 行数分布，大表默认不精确扫描）、错误日志扫描、基于 `coord(pid,vxid)` 的 CN/DN 跨节点日志关联。
- `diagnose`：SQLSTATE / 错误文本 / 日志文件（含跨节点关联）的证据化诊断，离线可用（不连数据库）。

**不支持**（刻意为之，均有验证依据，见 `references/error-cases.md`）：

- 依赖 `pg_stat_statements`、`pgxc_lock_for_backends`、`pg_current_setting`、`pg_file_settings`、EXECUTE DIRECT、csvlog global trace id——这些接口在本环境不存在、需 superuser 或默认关闭；
- 任何修改操作：节点启停、数据库重启、配置修改、权限修改、数据删除、事务清理。全部检查只读。

## 环境要求

- Python ≥ 3.10（开发验证：3.10.12 / 3.14.7）
- 数据库探测与巡检需要 `psycopg2`（`pip install -e '.[db]'`）；仅日志诊断/文本诊断无第三方依赖
- 目标环境：OpenTenBase v5.0 单 CN 或多节点集群；非 OpenTenBase 的 PG 环境只能跑通用子集（如文本诊断）

## 安装

```bash
cd otb-troubleshooting-toolkit
pip install -e '.[test]'        # 开发；仅使用则 '.[db]'
# 或不经安装直接运行：
PYTHONPATH=src python3 -m otb_tools.cli <subcommand> ...
```

实验 ECS（无 pip 安装）直接 `PYTHONPATH=src python3 -m otb_tools.cli`（已实测，Python 3.10.12 + psycopg2 2.9.13）。

## 用法

### 1. 部署环境预检

```bash
otb-tools preflight --expect-running \
  --conn "host=127.0.0.1 port=15001 dbname=lab user=<用户>" \
  --conf-files /data/opentenbase-lab/cn/data/postgresql.conf,/data/opentenbase-lab/dn1/data/postgresql.conf \
  --pg-hba-files /data/opentenbase-lab/cn/data/pg_hba.conf \
  --format json -o report.json
```

- 默认路径为实验环境（`/opt/opentenbase-lab/...`），全部可用选项覆盖；
- `--expect-running`：数据库应已运行（端口未监听/拒绝连接判 FAIL）；不加视为部署前检查（未运行属预期）；
- `--probe-env "LD_LIBRARY_PATH=/wrong/path"`：仅对 psql 子进程注入错误环境，用于复现动态库故障，不影响真实环境；
- 退出码：0 无 FAIL 级问题（WARN 允许）/ 1 存在 FAIL（`--strict` 下 WARN 也计 1）/ 2 执行失败或不完整 / 3 参数错误。

### 2. 集群巡检

```bash
otb-tools inspect --conn "host=127.0.0.1 port=15001 dbname=lab user=<用户>" \
  --log-dir /data/opentenbase-lab/logs --log-window-hours 24 --format json
```

DB 连接失败时其余节点级检查（进程/端口/日志）继续执行，依赖数据库的项标记 SKIP，退出码 2。

数据倾斜（`data.skew`）：对各分片表按 `xc_node_id` 统计各 DN 行数，最大/最小 ≥ 2 判 WARN；完全倾斜（存在 0 行 DN）且总行数 ≥ 1 万判 FAIL（否则降级 WARN）。不均衡不等于故障（不影响正确性，主要影响并行查询性能，也可能是有意为之）。安全设计：`reltuples` 超过 `--skew-max-rows`（默认 10 万）或未 ANALYZE（reltuples=-1）的表默认不做精确全扫，用 `pg_stats` 给出零扫描倾斜风险提示（仅提示，不参与判定）；`--skew-tables "schema.t,表名"` 可显式强制统计。

### 3. 诊断

```bash
otb-tools diagnose --sqlstate 42704
otb-tools diagnose --text "psql: error while loading shared libraries: libreadline.so.7: ..."
otb-tools diagnose --cn-logs /data/opentenbase-lab/logs/cn.log \
  --dn-logs /data/opentenbase-lab/logs/dn1.log,/data/opentenbase-lab/logs/dn2.log --window-hours 168
```

关联结果置信度：`confirmed`（双侧事件、时间窗口内）/ `candidate`（级别不足或疑似 PID 复用）/ `insufficient`（对侧无记录）。**关联置信度只说明两侧日志是否属于同一执行上下文，不等于故障根因已确认**（JSON 含 `root_cause` 字段显式区分）；证据不足时输出「无法确定根因」。

diagnose 退出码（离线分析语义，与 preflight/inspect 的健康检查语义不同）：0 = 分析成功完成（无论是否命中历史事件，命中历史事件不等于当前存在故障）/ 2 = 分析未能完整执行（如输入文件读取失败）/ 3 = 参数错误。`--strict` 为可选检测模式：命中 FAIL/WARN 签名或 confirmed 关联时返回 1，仅表示识别到异常事件。

文本诊断分层匹配：完整具体错误文本命中具体签名（confirmed）；短症状文本（如裸 `Connection refused`）命中通用类别签名（candidate，只识别类别不定位根因）；无命中 → insufficient。

## 输出样例

```text
[PASS] db.probe  连接成功（SELECT 1 = 1）
        evidence: exit_code=0
[WARN] conf.duplicate_guc  发现重复定义的参数
        evidence: /data/.../dn1/data/postgresql.conf: max_connections 定义于行 [64, 749]（后者生效）
        fix: 保留一处定义，删除其余
[FAIL] db.probe  psql 启动失败（动态库缺失）
        evidence: exit_code=127
        evidence: psql: error while loading shared libraries: libreadline.so.7: cannot open shared object file
```

每个检查项包含：`status`（PASS/WARN/FAIL/ERROR/SKIP）、`summary`、`evidence`（原始证据，含子进程退出码）、`facts`（已确认事实）、`possible_causes`（可能原因）、`hypotheses`（待验证假设）、`fix_hint`（修复建议）。JSON 顶层含 `exit_code`、`execution_status`（工具是否执行完成：ok/failed）与 `health_status`（环境/集群是否健康：healthy/degraded/failed/unknown），两者含义不同。结构见 `src/otb_tools/models.py` 的 `Report`/`Item`。

## 安全与只读保证

- 全部检查只读：DB 会话带 `default_transaction_read_only=on`；进程/端口/日志仅观察；无任何写操作路径；
- 子进程退出码（如 127）只进入 evidence，不映射为工具退出码；
- 连接串在输出前脱敏（password=***，支持 key=value 与 URI 两种形式）；仓库与报告不含真实凭据，连接凭据一律经 `--conn` 显式提供；
- 外部命令参数与 SQL 标识符均经安全处理（psql 探测用参数数组、节点名仅用于展示）。

## 已知限制

- 日志关联仅覆盖默认 stderr 格式（`log_line_prefix='%m [%p] '`）；csvlog 默认关闭，不支持 global trace id；
- 非 superuser 场景的采集接口受限（如无法读 pg_file_settings），已在错误案例库中记录替代方案；
- 日志量大时解析为全量读入内存（实验规模 ~18k 行无压力；超大日志建议外部先做时间裁剪）；
- `inspect` 的日志关联需要 CN/DN 日志文件均提供，缺一侧时如实标记 insufficient，不做推断。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q   # 86 项单元测试
```

集成验证（真实集群 8 场景 + 最终轮退出码回归 + Agent 端到端演示）见 `测试报告.md` 与 `演示记录.md`。
