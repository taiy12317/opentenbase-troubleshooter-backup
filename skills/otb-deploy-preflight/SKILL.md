---
name: otb-deploy-preflight
description: OpenTenBase 部署环境检查与安装/启动故障排查。当用户报告 OpenTenBase 装不起来、启动失败、psql 报动态库缺失（libreadline.so.7/libssl.so.1.1/libpq.so.5，退出码 127）、Connection refused、角色/数据库不存在，或要求做部署前环境检查、依赖检查时使用。调用 otb-tools preflight 只读检查二进制、LD_LIBRARY_PATH、libpq 符号链接、端口、目录、磁盘、配置文件重复参数、pg_hba 认证，输出 PASS/WARN/FAIL 分级、证据与修复建议。
---

# OpenTenBase 部署预检（otb-deploy-preflight）

## 用途

在部署/启动 OpenTenBase 之前或启动失败之后，用 `otb-tools preflight` 只读检查环境，逐项给出 PASS/WARN/FAIL、证据和修复建议。本 Skill 不做任何修改操作。

职责边界：本 Skill 只处理**部署前/启动前**的环境检查与启动失败排查。集群已运行时做状态巡检、连接失败分析、SQLSTATE 诊断、CN/DN 日志关联，用 otb-cluster-inspector。

## 前置条件

CLI 位于 OpenTenBase Agentic Troubleshooting Toolkit 仓库（`src/otb_tools/`），无第三方依赖即可运行（连接探测走 psql 子进程，不需要 psycopg2）。执行方式：

```bash
cd <toolkit 仓库根目录>
PYTHONPATH=src python3 -m otb_tools.cli preflight [选项]
```

实验 ECS 上的执行方式（集群在本机，工具装在服务器上；workbench 为课程环境专用，其他环境忽略本段）：

```bash
workbench exec -i <实例ID> -r cn-beijing --timeout 120 -c \
  'cd /data/opentenbase-lab/tools/otb-troubleshooting-toolkit && PYTHONPATH=src python3 -m otb_tools.cli preflight --format json'
```

## 调用时机

- 用户报告部署/启动失败、装不起来、依赖报错；
- 用户要求「检查部署环境」「装前检查」；
- psql/postgres 报 `error while loading shared libraries`（exit 127）；
- 需要确认端口、目录、磁盘、配置是否就绪。

## 关键选项

| 选项 | 作用 |
|---|---|
| `--pg-bin` | 二进制目录（默认 /opt/opentenbase-lab/pg/bin） |
| `--runtime-libpq` | runtime libpq 目录（默认 /opt/opentenbase-lab/runtime/libpq） |
| `--data-dirs` / `--log-dir` / `--ports` | 逗号分隔覆盖默认值 |
| `--conf-files` / `--pg-hba-files` | 逗号分隔配置文件路径（提供后检查重复参数/trust） |
| `--conn "host=... "` | 提供后追加部署后验证（仅连接探测）；运行期巡检（节点组/2PC/水位/日志关联）由 inspect 负责 |
| `--expect-running` | 数据库应已运行：端口未监听、连接拒绝判 FAIL；默认视为部署前（未运行属预期） |
| `--probe-env "LD_LIBRARY_PATH=/wrong/path"` | 用错误环境复现动态库故障（不改真实环境，只影响 psql 子进程） |
| `--strict` | WARN 也计入退出码 1 |
| `--format json\|text\|markdown` | 输出格式 |

## 结果解读

每个检查项含：`status`（PASS/WARN/FAIL/ERROR/SKIP）、`evidence`（原始证据，含子进程退出码）、`possible_causes`、`hypotheses`、`fix_hint`。

- 子进程退出码 127 可能是动态库缺失（LD_LIBRARY_PATH 不完整）或 psql 不在 PATH，工具按错误文本区分（`[Errno 2]` 且含 'psql' 为后者），2 = 连接失败，均只作为证据记录，不透传为工具退出码。
- 工具退出码：0 无 FAIL 级问题（WARN 允许）/ 1 存在 FAIL（--strict 下 WARN 也计 1）/ 2 执行失败或关键检查不完整 / 3 参数错误。JSON 输出含 `execution_status`（工具是否执行完成）与 `health_status`（环境是否就绪），两者含义不同。
- `auth.pg_hba_trust` 为 WARN 表示存在 trust 规则（密码校验未生效），实验室单机可接受，生产需评估。
- 检查项 SKIP 表示条件不满足（如未提供配置文件路径），不是故障。

## 约束

- 只读：不修改配置、不创建目录、不装依赖、不启停节点。
- 不把可选配置差异报为错误；参数差异用 WARN 并说明。
- 证据不足时如实说明，不编造原因。
- 错误签名知识库见仓库 `references/error-cases.md`（libreadline/libssl/libpq 缺失、Connection refused、28000/3D000、PGXC Group 未定义等，均来自真实复现）。
