# OpenTenBase Agentic Troubleshooting Toolkit

面向 OpenTenBase v5.0（Postgres-XL 架构）的只读运维排障工具集：一个共享 Python CLI（`otb-tools`，三个子命令）+ 两个 Qoder Agent Skill。把散落在 issue、日志与手工排查中的经验固化成可执行、可验证、Agent 可调用的检查与诊断工具。全部操作只读，不修改集群任何状态。

## 1. 组成

| 部件 | 位置 | 说明 |
|---|---|---|
| CLI `otb-tools` | `src/otb_tools/` | `preflight` / `inspect` / `diagnose` 三个子命令，共享结果模型与诊断规则 |
| Skill：otb-deploy-preflight | `skills/otb-deploy-preflight/` | 部署环境检查与安装/启动故障排查（Agent 调用入口） |
| Skill：otb-cluster-inspector | `skills/otb-cluster-inspector/` | 集群运行状态巡检与故障诊断（Agent 调用入口） |
| 故障知识库 | `references/error-cases.md` | 错误签名 / SQLSTATE / 跨节点关联机制，来源为真实复现与社区 issue 实证 |
| 独立预检工具 | `contrib-standalone/otb_preflight.py` | 零第三方依赖的单文件预检脚本（preflight 核心检查的独立实现） |

## 2. 安装

Python ≥ 3.9（开发验证于 3.10.12 / 3.14.7）。

```bash
git clone https://github.com/taiy12317/opentenbase-troubleshooter.git
cd opentenbase-troubleshooter
python3 -m pip install -e '.[db,test]'  # db=psycopg2（inspect 需要）；test=pytest（跑测试）
```

不经安装也可直接运行：

```bash
PYTHONPATH=src python3 -m otb_tools.cli <subcommand> ...
```

依赖按子命令区分：

- `preflight`：数据库连接探测通过 `psql` 子进程完成，不依赖 psycopg2（psql 需可用，缺失时该项明确报出）；
- `inspect`：数据库查询需要 `psycopg2`；
- `diagnose`：离线分析（SQLSTATE / 错误文本 / 日志），无第三方依赖。

> 本文示例中的默认路径（`/opt/opentenbase-lab/...`）与 `/data/opentenbase-lab/...` 是课程实验集群的部署布局，仅作示例。其他环境通过 `--pg-bin`、`--data-dirs`、`--log-dir`、`--ports` 等选项按实际部署覆盖。

## 3. 用法

### 3.1 部署环境预检（preflight）

```bash
otb-tools preflight --expect-running \
  --conn "host=127.0.0.1 port=15001 dbname=lab user=<用户>" \
  --conf-files /data/opentenbase-lab/cn/data/postgresql.conf,/data/opentenbase-lab/dn1/data/postgresql.conf \
  --pg-hba-files /data/opentenbase-lab/cn/data/pg_hba.conf \
  --format json -o report.json
```

检查项：二进制、LD_LIBRARY_PATH、动态库（ldd）、libpq 符号链接、端口、目录、磁盘、配置文件重复参数、pg_hba 认证、psql 数据库探测。

- 默认按部署前场景（不要求数据库运行）；`--expect-running` 表示数据库应已运行（端口未监听/连接拒绝判 FAIL）；
- 提供 `--conn` 时追加部署后验证（连接探测）；运行期巡检由 `inspect` 负责；
- `--probe-env "LD_LIBRARY_PATH=/wrong/path"` 仅对 psql 子进程注入错误环境，用于复现动态库故障，不影响真实环境。

### 3.2 集群巡检（inspect）

```bash
otb-tools inspect --conn "host=127.0.0.1 port=15001 dbname=lab user=<用户>" \
  --log-dir /data/opentenbase-lab/logs --log-window-hours 24 --format json
```

检查项：CN 连接、pgxc_node 注册、pgxc_group、注册端口 vs 实际监听、节点进程、CN→DN 直连、跨节点生效参数对比、会话与连接水位、pg_prepared_xacts、数据分布与倾斜、错误日志扫描、基于 `coord(pid,vxid)` 的 CN/DN 跨节点日志关联。

- 数据库连接失败时，进程/端口/日志等节点级检查继续执行，依赖数据库的项标记 SKIP，退出码 2；
- 数据倾斜（`data.skew`）：按 `xc_node_id` 统计各 DN 行数；最大/最小 ≥ 2 判 WARN；完全倾斜（存在 0 行节点）且总行数 ≥ 1 万判 FAIL，否则降级 WARN。行数不均衡影响性能与负载均衡，不影响查询正确性；分布节点范围元数据异常（nodeoids 无法映射、出现范围外/未注册节点）时只报告、不判定。超 `--skew-max-rows`（默认 10 万）或未 ANALYZE 的表默认不精确扫描，`--skew-tables` 可显式强制；
- 不依赖本环境不可用或默认关闭的接口（`pg_stat_statements`、`pgxc_lock_for_backends`、`pg_current_setting`、`pg_file_settings`、EXECUTE DIRECT、csvlog global trace id），替代方案见 `references/error-cases.md`；非 superuser 时部分采集接口受限，按知识库记录的替代方案处理。

### 3.3 诊断（diagnose）

```bash
otb-tools diagnose --sqlstate 42704
otb-tools diagnose --text "psql: error while loading shared libraries: libreadline.so.7: ..."
otb-tools diagnose --cn-logs /data/opentenbase-lab/logs/cn.log \
  --dn-logs /data/opentenbase-lab/logs/dn1.log,/data/opentenbase-lab/logs/dn2.log --window-hours 168
```

- 文本诊断分层匹配：完整具体错误文本命中具体签名（confirmed）；短症状文本（如裸 `Connection refused`）命中通用类别签名（candidate，只识别类别）；无命中时输出未匹配说明并给出进一步排查建议（可补充 SQLSTATE 或节点日志）；
- 日志关联置信度：`confirmed` 要求双侧事件在配对窗口内且 CN 来源身份无歧义；级别不足、疑似 PID 复用或同一 DN 配对窗口内出现多个不同 CN 来源时标为 `candidate`；对侧无记录为 `insufficient`。`diagnose` 未提供 CN 节点名时以日志来源文件区分；多文件轮转与多 CN 无法区分时保守标为候选，不按文件名猜测节点身份。日志关联用于识别跨节点异常事件，错误签名用于提供可能原因及排查建议，结合运行状态、错误上下文及验证结果定位具体原因（JSON 的 `root_cause` 字段显式区分）；
- 日志关联基于默认 stderr 格式的 `coord(pid,vxid)` 字段；需要 CN/DN 日志均提供，单侧日志关联标记为 `insufficient`；日志解析为全量读入内存（1000 万行 / 1.1GB 实测可处理，耗时与内存见 `测试报告.md` 第九章）。

### 3.4 退出码

`preflight` / `inspect`（健康检查语义）：

| 码 | 含义 |
|---|---|
| 0 | 检查完成且无 FAIL（WARN 不计；`--strict` 时 WARN 计入 1） |
| 1 | 存在 FAIL |
| 2 | 存在 ERROR（关键检查未完成或证据不完整） |
| 3 | 参数错误 |

`diagnose`（离线分析语义）：

| 码 | 含义 |
|---|---|
| 0 | 分析完成（历史事件命中与否不影响退出码） |
| 1 | `--strict` 检测模式且命中 FAIL/WARN 级签名或 confirmed 关联 |
| 2 | 证据不完整（输入读取失败或日志全部行无法解析） |
| 3 | 参数错误 |

## 4. Qoder Skill 安装与调用

把仓库内的两个 Skill 复制到 Qoder 用户技能目录并重载：

```bash
cp -r skills/otb-deploy-preflight ~/.qoder-cn/skills/
cp -r skills/otb-cluster-inspector ~/.qoder-cn/skills/
```

在 Qoder 会话中执行 `/skills reload`（或重启会话）。之后 Agent 会按触发场景自动调用，也可显式 `/<skill-name>` 调用。面向 Agent 的调用规则（触发条件、参数、结果解读）见各 SKILL.md；仓库内版本与 CLI 行为保持一致，修改后需重新同步到 `~/.qoder-cn/skills/`。

## 5. 输出与结果模型

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

每个检查项包含：`status`（PASS/WARN/FAIL/ERROR/SKIP）、`summary`、`evidence`（原始证据，含子进程退出码）、`facts`（已确认事实）、`possible_causes`（可能原因）、`hypotheses`（待验证假设）、`fix_hint`（修复建议）。JSON 顶层含 `exit_code`、`execution_status`（工具是否执行完成：ok/failed）与 `health_status`（环境/集群健康状态：healthy/degraded/failed/unknown），两者含义不同。结构定义见 `src/otb_tools/models.py` 的 `Report` / `Item`。

## 6. 只读与安全保证

- 检查按只读方式执行：`inspect` 数据库会话设置 `default_transaction_read_only=on`；`preflight` 仅执行固定的 `SELECT 1`；进程/端口/日志检查仅读取；
- 连接串在输出前脱敏（password=***，支持 key=value 与 URI 两种形式）；仓库与报告不含真实凭据，连接凭据一律经 `--conn` 显式提供；
- psql 探测将完整连接串作为单一 `--dbname=<conninfo>` 值传入，避免其中以 `-` 开头的内容变成 psql 选项；只有退出码为 0 且标准输出恰为 `1` 才判定连接成功。
- pg_hba 检查按 `local` / `host*` 记录字段位置识别认证方法，支持行内注释、双引号字段与独立 netmask；它不替代数据库自身对配置语法的完整校验。

## 7. 测试

```bash
.venv/bin/python -m pytest tests/ -q   # 2026-11-26：242 passed
```

集成验证（真实集群 8 场景 + 数据倾斜检查）见 `测试报告.md`；实际使用场景见 `演示记录.md`。

## 8. 仓库结构

| 路径 | 内容 |
|---|---|
| `src/otb_tools/` | CLI 实现（结果模型、诊断规则、日志解析、检查逻辑） |
| `skills/` | 两个 Qoder Skill（各含 SKILL.md） |
| `references/error-cases.md` | 故障知识库 |
| `contrib-standalone/otb_preflight.py` | 零依赖单文件预检脚本 |
| `examples/` | diagnose 离线诊断示例（含固定日志样例，见 [样例说明](examples/README.md)） |
| `tests/` | 单元测试（242 项） |
| `测试报告.md` / `演示记录.md` | 测试结果与使用场景 |
