"""诊断规则：已知失败签名匹配 + SQLSTATE 案例表。

所有条目均来自真实证据：
  - 本实验集群实测复现的（标注 reproduced）；
  - OpenTenBase 源码核验的（标注 source-verified）；
  - 社区 Issue/PR 记录的（标注 community-evidence，未经本环境复现）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 每项：id / 匹配正则 / 分类 / 严重度 / 可能原因 / 下一步 / 证据来源 / 匹配层级
# layer=specific 的签名基于具体错误文本（复现/源码/社区实证）；
# layer=generic 的短文本签名只识别错误类别，不定位根因（specific 命中时被覆盖）。
@dataclass
class Signature:
    id: str
    pattern: str
    category: str
    severity: str
    causes: list[str]
    next_steps: list[str]
    evidence: str
    layer: str = "specific"
    _re: re.Pattern = field(init=False, repr=False)

    def __post_init__(self):
        self._re = re.compile(self.pattern, re.IGNORECASE)

    def match(self, text: str):
        return self._re.search(text)

    def to_dict(self, text: str):
        m = self.match(text)
        groups = m.groupdict() if m else {}
        return {
            "id": self.id,
            "category": self.category,
            "severity": self.severity,
            "possible_causes": self.causes,
            "next_steps": self.next_steps,
            "evidence_basis": self.evidence,
            "layer": self.layer,
            "matched_fields": {k: v for k, v in groups.items() if v is not None},
        }


SIGNATURES: list[Signature] = [
    Signature(
        id="dynlib-missing",
        pattern=r"error while loading shared libraries: (?P<lib>\S+): cannot open shared object file",
        category="dynamic_library",
        severity="FAIL",
        causes=[
            "LD_LIBRARY_PATH 未设置或不完整，缺少兼容库目录（如 runtime/libpq、compat/lib、compat/usr/lib）",
            "对应动态库未安装或符号链接损坏",
        ],
        next_steps=[
            "执行 source <安装目录>/env.sh（或等价环境脚本）后重试",
            "用 ldd <pg_bin>/postgres 核对缺失库及其期望路径",
        ],
        evidence="reproduced: 实验集群实测 exit 127，缺失 libreadline.so.7 / libssl.so.1.1 / libcrypto.so.1.1",
    ),
    Signature(
        id="libpq-symlink-mismatch",
        pattern=r"Expected OpenTenBase libpq\.so\.5\.10 symlink not found",
        category="dynamic_library",
        severity="FAIL",
        causes=["runtime/libpq/libpq.so.5 未指向 pg/lib/libpq.so.5.10"],
        next_steps=["ln -sf $(readlink -f <pg_home>/lib/libpq.so.5.10) <runtime_libpq>/libpq.so.5"],
        evidence="reproduced: start_lab.sh 首段即校验该符号链接；错误环境变量时实测触发",
    ),
    Signature(
        id="connection-refused",
        pattern=r"could not connect to server: Connection refused",
        category="connectivity",
        severity="FAIL",
        causes=[
            "客户端到目标 host/port 的连接被拒绝：目标端口无进程监听（节点未启动/已崩溃）",
            "连接了错误的 host/port（连接串与实际部署不一致）",
            "网络路径问题（防火墙/容器网络/隧道未建立）",
        ],
        next_steps=[
            "核对用户实际连接的 host/port 与目标节点配置",
            "确认报错发生在客户端侧还是服务器侧",
            "服务器侧 ss -ltn 核对监听；服务器本机 LISTEN 与客户端可达是两件独立事实，须分别确认",
            "从实际客户端环境重试连接；核对节点状态（pg_ctl/gtm_ctl status）与启动日志",
        ],
        evidence="reproduced: 实验集群连不存在端口实测该错误（exit 2）；同一时刻集群其余端口正常",
    ),
    Signature(
        id="connection-refused-generic",
        pattern=r"Connection refused",
        category="connectivity",
        severity="WARN",
        layer="generic",
        causes=[
            "连接被拒绝：需结合端口监听状态与实际连接串区分节点宕机、端口配置错误或网络故障",
            "常见可能：目标端口无监听、host/port 配置错误、网络/防火墙/转发不可达",
        ],
        next_steps=[
            "提供完整错误文本（含 psql 前缀）与实际连接串的 host/port",
            "确认报错发生在客户端侧还是服务器侧；服务器本机 LISTEN 与客户端可达须分别确认",
            "核对目标端口监听状态，并从实际客户端环境重试",
        ],
        evidence="generic: 短文本仅能识别错误类别，具体原因需补充完整错误文本与连接串信息",
    ),
    Signature(
        id="role-not-exists",
        pattern=r'role "(?P<role>\S+)" does not exist',
        category="auth",
        severity="FAIL",
        causes=["连接串中的用户名不存在", "用户名拼写错误"],
        next_steps=["用超级用户 CREATE ROLE 或改用已存在角色", "SELECT rolname FROM pg_roles 核对"],
        evidence="reproduced: 实验集群实测 SQLSTATE 28000, exit 2",
    ),
    Signature(
        id="database-not-exists",
        pattern=r'database "(?P<db>\S+)" does not exist',
        category="auth",
        severity="FAIL",
        causes=["目标数据库未创建", "连接串库名拼写错误"],
        next_steps=["createdb 或改用已存在数据库", "SELECT datname FROM pg_database 核对"],
        evidence="reproduced: 实验集群实测 SQLSTATE 3D000, exit 2",
    ),
    Signature(
        id="pgxc-group-not-defined",
        pattern=r"PGXC Group (?P<group>\S+): group not defined",
        category="distribution",
        severity="FAIL",
        causes=["DDL 指定的节点组不存在", "集群初始化时 default_group 未正确创建"],
        next_steps=["SELECT * FROM pgxc_group 核对现有节点组", "改用 default_group 或先创建节点组"],
        evidence="reproduced: 实验集群实测 SQLSTATE 42704（OpenTenBase 特有文本）",
    ),
    Signature(
        id="default-group-not-defined",
        pattern=r"default group not defined",
        category="distribution",
        severity="FAIL",
        causes=["节点组元数据缺失/损坏，常见于初始化不完整或手动改元数据"],
        next_steps=["核对 pgxc_group 与 pgxc_node 注册", "必要时重建节点组（需运维确认）"],
        evidence="reproduced: 实验集群 cn.log 中实测出现该 ERROR 行",
    ),
    Signature(
        id="recovery-mode",
        pattern=r"the database system is in recovery mode",
        category="node_state",
        severity="FAIL",
        causes=["节点处于崩溃恢复中", "备节点被当作主节点访问"],
        next_steps=["等待恢复完成", "检查该节点日志中的 recovery 进度"],
        evidence="reproduced: 实验集群 cn.log 中实测出现该 FATAL 行",
    ),
    Signature(
        id="admin-termination",
        pattern=r"terminating connection due to administrator command",
        category="node_state",
        severity="WARN",
        causes=["管理员执行 pg_terminate_backend", "节点停机前的连接清理", "对端 tcp 失败触发的强制清理（常与 force kill 伴随出现）"],
        next_steps=["若伴随 tcp fails to send data：定位对端节点故障，做跨节点日志关联", "若无伴随：确认是否有管理操作"],
        evidence="reproduced: 实验集群 dn1/dn2.log 实测出现该 FATAL 行（停节点时）",
    ),
    Signature(
        id="tcp-send-fail-kill",
        pattern=r"because tcp fails to send data, force kill SIGTERM to pid (?P<pid>\d+)",
        category="network",
        severity="FAIL",
        causes=["对端（CN 或 DN）进程已退出/网络中断，本端收不到确认"],
        next_steps=["用被 kill 的 pid 在对端日志中查找对应事件（coord 关联）", "检查对端节点是否宕机"],
        evidence="reproduced: 实验集群 dn1/dn2.log 实测出现该 WARNING 行",
    ),
    Signature(
        id="conn-state-change",
        pattern=r"Changing connection state for node (?P<node>\S+) pid: (?P<pid>\d+), state (?P<from>\w+) ==> (?P<to>\w+), invoker execRemote\.c: \d+",
        category="network",
        severity="WARN",
        causes=["CN 与 DN 的连接发生状态迁移（ERROR/FATAL 迁移说明 DN 侧出错）"],
        next_steps=["在 DN 日志中用 pid 查找同时间事件（coord 关联）"],
        evidence="reproduced: 实验集群 cn.log 实测出现（execRemote.c:862）",
    ),
    Signature(
        id="syntax-error",
        pattern=r"syntax error at or near",
        category="sql_error",
        severity="FAIL",
        causes=["SQL 语句语法错误（SQLSTATE 42601）"],
        next_steps=["检查报错位置附近的 SQL 文本"],
        evidence="reproduced: 实验集群实测 SQLSTATE 42601",
    ),
    Signature(
        id="relation-not-exists",
        pattern=r'relation "(?P<rel>\S+)" does not exist',
        category="sql_error",
        severity="FAIL",
        causes=["查询的表/视图不存在（SQLSTATE 42P01）", "表在别的 schema/数据库"],
        next_steps=["psql 内 \\dt 核对表名与 schema", "检查 search_path"],
        evidence="reproduced: 实验集群实测 SQLSTATE 42P01",
    ),
    Signature(
        id="type-not-exists",
        pattern=r'type "(?P<name>\S+)" does not exist',
        category="sql_error",
        severity="FAIL",
        causes=[
            "当前 search_path 可见范围内不存在该名称的类型（SQLSTATE 42704，名称解析失败类别）",
            "对象存在于其他 schema 但未被 search_path 覆盖，或缺少 schema 限定",
            "对象确实不存在（名称拼写、大小写或目标库不对）",
        ],
        next_steps=[
            "SELECT n.nspname, t.typname, t.typtype FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE t.typname = '<name>' 核对类型是否存在及其所在 schema",
            "核对 SHOW search_path 与原始 SQL 是否带 schema 限定",
            "注意：普通表会自动创建同名复合类型；type \"x\" does not exist 不表示该表不存在，也不表示它不是表",
        ],
        evidence="reproduced: 实验集群实测——SET search_path=pg_catalog 后 'memories'::regtype 报 42704 type \"memories\" does not exist，而 public 中同名复合类型实际存在（由同名表自动创建）",
    ),
    Signature(
        id="division-by-zero",
        pattern=r"division by zero",
        category="sql_error",
        severity="FAIL",
        causes=["SQL 计算中除数为 0（SQLSTATE 22012）"],
        next_steps=["检查数据中 0 值/空值，考虑 NULLIF/CASE"],
        evidence="reproduced: 实验集群实测 SQLSTATE 22012",
    ),
    Signature(
        id="permission-denied",
        pattern=r"permission denied for (?P<what>.+)",
        category="sql_error",
        severity="FAIL",
        causes=["当前角色对对象/视图/文件无权限（SQLSTATE 42501）"],
        next_steps=["GRANT 或改用具备权限的角色", "注意 OpenTenBase 普通用户无法读 pg_file_settings"],
        evidence="reproduced: 实验集群 student 读 pg_file_settings 实测 42501",
    ),
    Signature(
        id="function-not-exists",
        pattern=r"function (?P<fn>[^(]+)\([^)]*\) does not exist",
        category="sql_error",
        severity="FAIL",
        causes=["调用了不存在的函数（SQLSTATE 42883）", "OpenTenBase 无 pg_current_setting（须用 current_setting）"],
        next_steps=["psql 内 \\df 核对函数名与参数", "OpenTenBase 环境优先使用 current_setting()"],
        evidence="reproduced: 实验集群实测 pg_catalog.pg_current_setting 报 42883",
    ),
    Signature(
        id="too-many-connections",
        pattern=r"remaining connection slots are reserved",
        category="capacity",
        severity="FAIL",
        causes=["连接数达到 max_connections 上限（SQLSTATE 53300）"],
        next_steps=["检查连接水位与空闲会话", "清理空闲连接或调大 max_connections（需重启）"],
        evidence="community-evidence: PostgreSQL 通用行为，本环境未复现",
    ),
    Signature(
        id="deadlock-detected",
        pattern=r"deadlock detected",
        category="concurrency",
        severity="FAIL",
        causes=["事务互相等待形成死锁（SQLSTATE 40P01）"],
        next_steps=["查看日志中死锁详情与涉及事务", "检查应用加锁顺序"],
        evidence="community-evidence: 本环境未复现",
    ),
    Signature(
        id="failed-to-send-plan",
        pattern=r"Failed to send plan",
        category="network",
        severity="FAIL",
        causes=["分布式查询期间 CN→DN 连接失效（DN 后端被终止/DN 不可用），执行计划无法下发"],
        next_steps=["在 DN 日志中查找同一时刻 57P01 FATAL（terminating connection due to administrator command）并做 coord 关联", "核对 DN 节点状态"],
        evidence="reproduced: 2026-11-23 本环境实测——终止自身会话的 DN 后端后，CN 客户端报 08000 Failed to send plan；CN/DN 日志同 coord(8307,3408) 配对",
    ),
    Signature(
        id="failed-to-get-pooled-connections",
        pattern=r"Failed to get pooled connections",
        category="network",
        severity="FAIL",
        causes=["CN 无法从连接池获取到 DN 的连接（DN 停机/不可达/连接池耗尽）"],
        next_steps=["从错误文本定位节点（connect for dnXXXX(host:port) failed）并核对端口/进程", "inspect 直连该 DN 验证可达性；HINT 提示区分节点故障与连接池配置不足"],
        evidence="reproduced: 2026-11-24 本环境实测——fast 停 DN1 后，CN 客户端分布式查询报 53000 Failed to get pooled connections: connect for dn0001(127.0.0.1:15401) failed, errmsg: could not connect to server: Connection refused；重启 DN1 后恢复",
    ),
    Signature(
        id="dn-connection-failed",
        pattern=r"Invalid Datanode|node .* is not healthy",
        category="network",
        severity="FAIL",
        causes=["CN 对 DN 连接失效（DN 宕机/网络中断）"],
        next_steps=["核对各 DN 端口监听与直连可达性", "检查 DN 日志"],
        evidence="community-evidence: Issue #341（DN 连接失效）与 #194（57P01）；同类场景本环境实测两类客户端表现：杀 DN 后端 → 08000 Failed to send plan（见 failed-to-send-plan 签名）；停 DN → 53000 Failed to get pooled connections（见 failed-to-get-pooled-connections 签名）",
    ),
    Signature(
        id="xx000-not-supported",
        pattern=r"not support global index yet|TRIGGER is not supported on replication table",
        category="sql_error",
        severity="FAIL",
        causes=[
            "OpenTenBase 用 elog(ERROR) 报告不支持的功能，SQLSTATE 被误报为 XX000 internal_error（应为 0A000 feature_not_supported，Issue #507）",
        ],
        next_steps=[
            "按 0A000 语义理解：该功能未实现或分布式模式不支持，换用支持的方式",
            "关注 Issue #507 与 PR #508/#545 的修复进展",
        ],
        evidence="reproduced: 2026-11-25 实验集群实测 CREATE GLOBAL INDEX 报 (XX000) not support global index yet；复制表 CREATE TRIGGER 报 (XX000) TRIGGER is not supported on replication table in distributed mode",
    ),
]


# SQLSTATE 案例表。otb_note 里区分 reproduced / community-evidence。
SQLSTATE_TABLE: dict[str, dict] = {
    "42601": {"name": "syntax_error", "causes": ["SQL 语法错误"], "next": ["检查 SQL 文本与报错位置"], "otb_note": "reproduced: 实验集群实测"},
    "42P01": {"name": "undefined_table", "causes": ["表/视图不存在或 schema 不对"], "next": ["核对表名与 search_path"], "otb_note": "reproduced: 实验集群实测"},
    "22012": {"name": "division_by_zero", "causes": ["除数为 0"], "next": ["检查数据，考虑 NULLIF/CASE"], "otb_note": "reproduced: 实验集群实测"},
    "42883": {"name": "undefined_function", "causes": ["函数不存在或参数不匹配", "OpenTenBase 环境注意：无 pg_current_setting()，须用 current_setting()"], "next": ["psql 内 \\df 核对函数名与参数"], "otb_note": "reproduced: 实验集群实测 pg_current_setting 缺失报 42883"},
    "42501": {"name": "insufficient_privilege", "causes": ["角色权限不足"], "next": ["GRANT 或换角色；普通用户读不到 pg_file_settings"], "otb_note": "reproduced: 实验集群实测 pg_file_settings 42501"},
    "28000": {"name": "invalid_authorization_specification", "causes": ["角色不存在或认证失败"], "next": ["核对用户名与 pg_hba 规则"], "otb_note": "reproduced: 实验集群实测 role does not exist"},
    "3D000": {"name": "invalid_catalog_name", "causes": ["数据库不存在"], "next": ["核对库名"], "otb_note": "reproduced: 实验集群实测"},
    "42704": {
        "name": "undefined_object",
        "causes": [
            "当前 search_path 可见范围内不存在所引用的对象（对象可能存在于其他 schema）",
            "对象确实不存在（名称拼写、大小写或目标库错误）",
            "OpenTenBase 特有文本：PGXC Group ...: group not defined（节点组未定义）",
        ],
        "next": [
            "按对象类型查 pg_class/pg_type/pg_proc/pg_namespace 核对对象是否存在及其 schema",
            "核对 SHOW search_path 与原始 SQL 是否带 schema 限定",
            "PGXC Group 场景核对 pgxc_group",
        ],
        "otb_note": "reproduced: 实验集群实测两类 42704——类型在 search_path 外（SET search_path=pg_catalog 后 'memories'::regtype 报 type \"memories\" does not exist）与 PGXC Group not defined；42704 为名称解析失败类别，不指向单一根因",
    },
    "53300": {"name": "too_many_connections", "causes": ["连接数超上限"], "next": ["检查连接水位"], "otb_note": "community-evidence: 本环境未复现"},
    "40P01": {"name": "deadlock_detected", "causes": ["事务死锁"], "next": ["查看日志死锁详情"], "otb_note": "community-evidence: 本环境未复现"},
    "57P01": {
        "name": "admin_shutdown",
        "causes": [
            "节点被管理命令停止（pg_ctl stop / 停机维护等正常管理操作）",
            "Issue #194：分布式查询期间 DN 不可用也可表现为 57P01——57P01 不必然表示管理员主动停节点",
        ],
        "next": [
            "核对节点状态与启停记录；检查 CN 日志 execRemote 状态迁移",
            "分布式查询报错时联合看 CN/DN 两侧日志：DN 侧 57P01 FATAL 在客户端可能表现为 08000 Failed to send plan（本环境实测）",
        ],
        "otb_note": "reproduced: 2026-11-23 本环境实测 pg_terminate_backend 终止自身会话 → 客户端 psql 收到 FATAL (57P01) terminating connection due to administrator command，服务器日志同步 FATAL；另实测分布式查询期间杀自身会话的 DN 后端 → CN 客户端报 08000 Failed to send plan，DN 日志为 57P01 FATAL（Issue #194 客户端表现）",
    },
    "XX000": {"name": "internal_error", "causes": ["内核内部错误（需结合日志上下文判断具体原因）", "Issue #507：部分「不支持的功能」被 elog(ERROR) 误报为 XX000（应为 0A000 feature_not_supported）——XX000 不必然是内部故障；本环境实测复现：CREATE GLOBAL INDEX 报 not support global index yet、复制表 CREATE TRIGGER 报 TRIGGER is not supported（2026-11-25）"], "next": ["提取完整日志与查询上下文", "若报错内容实为功能不支持（如 not support global index yet），按 0A000 语义理解，换用支持的方式；关注 Issue #507 与 PR #508/#545 修复进展"], "otb_note": "reproduced: 2026-11-25 实验集群实测复现 Issue #507 两条触发语句——CREATE GLOBAL INDEX 报 (XX000) not support global index yet；复制表 CREATE TRIGGER 报 (XX000) TRIGGER is not supported on replication table in distributed mode（均为不支持功能被 elog 误报，应报 0A000）"},
    "08000": {
        "name": "connection_exception",
        "causes": [
            "分布式查询期间 CN→DN 连接失效：DN 后端被终止/节点不可用（本环境实测：杀 DN 后端后 CN 客户端报 08000 Failed to send plan）",
            "客户端与服务器之间连接中断/网络故障",
        ],
        "next": [
            "在 DN 日志中查找同一时刻的 57P01 FATAL（terminating connection due to administrator command）",
            "核对 DN 节点状态与 CN 日志 Failed to send plan 行的 coord 关联",
        ],
        "otb_note": "reproduced: 2026-11-23 本环境实测——终止自身会话的 DN 后端后，CN 客户端分布式查询报 08000 Failed to send plan，DN 日志同步 57P01 FATAL（coord 8307,3408 双侧配对）",
    },
    "53000": {
        "name": "insufficient_resources",
        "causes": [
            "分布式查询期间 DN 停机/不可达：CN 报 Failed to get pooled connections（本环境实测：fast 停 DN1 后 CN 查询报 53000，HINT 提示 one or more nodes are currently unreachable）",
            "连接池耗尽/配置不足（pooler_scale_factor、max_pool_size 过小）",
        ],
        "next": [
            "从错误文本定位具体节点（connect for dnXXXX(host:port) failed），核对该节点端口与进程",
            "结合 HINT 区分节点故障与连接池配置不足；inspect 直连该 DN 验证可达性",
        ],
        "otb_note": "reproduced: 2026-11-24 本环境实测——fast 停 DN1 后，CN 客户端分布式查询报 53000 Failed to get pooled connections: connect for dn0001(127.0.0.1:15401) failed, errmsg: could not connect to server: Connection refused；与 08000（DN 后端被杀）为不同故障路径",
    },
}


def match_signatures(text: str) -> list[dict]:
    """返回命中的所有签名（含匹配字段）。"""
    out = []
    for sig in SIGNATURES:
        if sig.match(text):
            out.append(sig.to_dict(text))
    return out


def sqlstate_case(code: str) -> dict | None:
    return SQLSTATE_TABLE.get(code.upper())


def lookup_by_category(text: str) -> list[dict]:
    """按文本匹配签名后去重分类，用于日志分类统计。"""
    cats = []
    for sig in SIGNATURES:
        if sig.match(text):
            cats.append(sig.category)
    return sorted(set(cats))
