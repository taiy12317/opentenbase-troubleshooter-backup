#!/usr/bin/env python3
"""otb_preflight.py - OpenTenBase 部署环境只读预检（单文件、无第三方依赖）。

面向 OpenTenBase v5.0（Postgres-XL 架构）的部署前/启动失败后环境检查：
  - LD_LIBRARY_PATH 与 compat 目录（缺失时报 libreadline/libssl 无法加载，exit 127）
  - 工具二进制（postgres/psql/gtm_ctl/pg_ctl）
  - postgres 动态库依赖（ldd）
  - runtime libpq.so.5 符号链接（start_lab.sh 首段即校验该链接）
  - 端口占用（部署前 vs --expect-running 两种语义）
  - 数据/日志/配置目录、磁盘空间
  - postgresql.conf 重复参数（后者生效）、pg_hba trust 规则
  - psql 数据库探测（可选，--conn），探测子进程退出码 127/2 等保留为证据

全部检查只读，不修改任何环境。退出码：0 无 FAIL 级问题（WARN 允许；--strict 时 WARN 计 1）
/ 1 存在 FAIL / 2 执行失败或不完整 / 3 参数错误。
已验证环境：Ubuntu 22.04 + OpenTenBase v5.0 隔离实验集群（实测样本与案例见 references/error-cases.md）。

用法示例：
  python3 otb_preflight.py --expect-running --conn "host=127.0.0.1 port=15001 dbname=lab user=student"
  python3 otb_preflight.py --conf-files /data/opentenbase-lab/cn/data/postgresql.conf --pg-hba-files /data/opentenbase-lab/cn/data/pg_hba.conf
  python3 otb_preflight.py --probe-env "LD_LIBRARY_PATH=/wrong/path"   # 复现动态库故障，不影响真实环境
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

DEFAULT_PG_BIN = "/opt/opentenbase-lab/pg/bin"
DEFAULT_RUNTIME_LIBPQ = "/opt/opentenbase-lab/runtime/libpq"
DEFAULT_COMPAT_DIRS = [
    "/opt/opentenbase-lab/compat/usr/lib/x86_64-linux-gnu",
    "/opt/opentenbase-lab/compat/lib/x86_64-linux-gnu",
]
DEFAULT_DATA_DIRS = [
    "/data/opentenbase-lab/cn/data",
    "/data/opentenbase-lab/dn1/data",
    "/data/opentenbase-lab/dn2/data",
    "/data/opentenbase-lab/gtm",
]
DEFAULT_LOG_DIR = "/data/opentenbase-lab/logs"
DEFAULT_PORTS = [5000, 15001, 15401, 15402]

# 失败签名：pattern -> (id, 可能原因, 下一步)。均来自真实复现或社区 issue 实证。
SIGNATURES = [
    (
        r"error while loading shared libraries: (\S+)",
        "dynlib-missing",
        ["LD_LIBRARY_PATH 未包含 compat/runtime 目录，或对应依赖库缺失"],
        ["source 安装目录 env.sh 后重试（实验环境为 /opt/opentenbase-lab/env.sh）"],
    ),
    (
        r"Connection refused",
        "connection-refused",
        ["目标节点未启动、端口配置不一致或网络不可达"],
        ["用 pg_ctl/gtm_ctl status 核对节点；核对端口与 pgxc_node 注册"],
    ),
    (
        r'role "(\S+)" does not exist',
        "role-not-exists",
        ["连接用户未创建"],
        ["以超级用户创建角色或核对连接串 user 字段"],
    ),
    (
        r'database "(\S+)" does not exist',
        "database-not-exists",
        ["目标数据库未创建"],
        ["createdb 创建或核对连接串 dbname 字段"],
    ),
    (
        r"PGXC Group (\S+): group not defined",
        "pgxc-group-not-defined",
        ["连接串 group 选项引用了不存在的节点组"],
        ["核对 pgxc_group；连接串不指定 group 或改用 default_group"],
    ),
    (
        r"default group not defined",
        "default-group-not-defined",
        ["default_group 节点组缺失（GTM/节点注册异常）"],
        ["检查 pgxc_group 与节点注册状态"],
    ),
    (
        r"the database system is in recovery mode",
        "recovery-mode",
        ["节点处于恢复状态"],
        ["等待恢复完成或查看节点日志"],
    ),
    (
        r"remaining connection slots are reserved",
        "too-many-connections",
        ["连接数达到上限"],
        ["核对 max_connections 与当前会话数；排查连接泄漏"],
    ),
]

STATUS_PASS, STATUS_WARN, STATUS_FAIL, STATUS_ERROR, STATUS_SKIP = "PASS WARN FAIL ERROR SKIP".split()


class Item:
    def __init__(self, item_id, name, status=STATUS_PASS):
        self.id = item_id
        self.name = name
        self.status = status
        self.summary = ""
        self.evidence = []
        self.causes = []
        self.fix_hint = ""

    def line(self):
        out = [f"[{self.status}] {self.id}  {self.summary}"]
        for e in self.evidence:
            out.append(f"        evidence: {e}")
        for c in self.causes:
            out.append(f"        cause: {c}")
        if self.fix_hint:
            out.append(f"        fix: {self.fix_hint}")
        return "\n".join(out)


def _run(cmd, env=None, timeout=10):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        # 不回显命令行：连接串可能含密码
        return 124, "", f"command timed out after {timeout}s"


def _match_signatures(text):
    hits = []
    for pattern, sid, causes, next_steps in SIGNATURES:
        if re.search(pattern, text):
            hits.append({"id": sid, "possible_causes": causes, "next_steps": next_steps})
    return hits


def _ss_listening_ports():
    rc, out, err = _run(["ss", "-ltn"], timeout=5)
    if rc != 0:
        raise OSError(f"ss 不可用: {err or out}")
    ports = set()
    for line in out.splitlines():
        for m in re.finditer(r":(\d+)\s", line):
            ports.add(int(m.group(1)))
    return ports


def _redact_conninfo(conninfo):
    """脱敏连接串中的密码，支持 key=value 与 postgresql:// URI 两种形式。"""
    if conninfo.startswith(("postgresql://", "postgres://")):
        try:
            parts = urllib.parse.urlsplit(conninfo)
            netloc = parts.netloc
            if "@" in netloc:
                userinfo, host = netloc.rsplit("@", 1)
                username, separator, _password = userinfo.partition(":")
                netloc = f"{username}{':***' if separator else ''}@{host}"
            query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            query = [
                (key, "***" if key.lower() in {"password", "sslpassword"} else value)
                for key, value in query
            ]
            return urllib.parse.urlunsplit(
                (parts.scheme, netloc, parts.path, urllib.parse.urlencode(query), "")
            )
        except ValueError:
            return "<redacted connection URI>"

    def redact_dsn(match):
        prefix = match.group(1)
        value = match.group(0)[len(prefix):]
        if value.startswith(("'", '"')):
            return f"{prefix}{value[0]}***{value[0]}"
        return f"{prefix}***"

    return re.sub(r"(?i)(?<!\S)(password\s*=\s*)(?:'(?:[^']|\\')*'|\"(?:[^\"]|\\\")*\"|(?:\\.|[^\s])*)", redact_dsn, conninfo)


def check_env_ld_library_path(args):
    item = Item("env.ld_library_path", "LD_LIBRARY_PATH 环境变量")
    val = os.environ.get("LD_LIBRARY_PATH", "")
    if not val:
        item.status = STATUS_WARN
        item.summary = "LD_LIBRARY_PATH 未设置"
        item.causes = ["运行 psql/postgres 可能报 libreadline.so.7 / libssl.so.1.1 缺失（exit 127）"]
        item.fix_hint = "source <安装目录>/env.sh 后再运行（实验环境为 /opt/opentenbase-lab/env.sh）"
        return item
    item.evidence = [f"LD_LIBRARY_PATH={val}"]
    item.summary = f"已设置（{len(val.split(':'))} 个目录）"
    for d in args.compat_dirs:
        if d and d not in val:
            item.status = STATUS_WARN
            item.summary = f"缺少建议目录 {d}"
    return item


def check_binaries(args):
    item = Item("bin.tools", "数据库工具二进制")
    for tool in ["postgres", "psql", "gtm_ctl", "pg_ctl"]:
        p = os.path.join(args.pg_bin, tool)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            item.evidence.append(f"存在且可执行: {p}")
        else:
            item.status = STATUS_FAIL
            item.evidence.append(f"缺失或不可执行: {p}")
    item.summary = "工具链完整" if item.status == STATUS_PASS else "存在缺失的工具"
    if item.status == STATUS_FAIL:
        item.causes = [f"{args.pg_bin} 下缺少对应二进制，安装不完整或 --pg-bin 参数错误"]
        item.fix_hint = f"核对 --pg-bin（当前 {args.pg_bin}）或补装 OpenTenBase"
    return item


def check_ldd(args):
    item = Item("dynlib.ldd", "postgres 动态库依赖")
    postgres = os.path.join(args.pg_bin, "postgres")
    if not os.path.isfile(postgres):
        item.status = STATUS_SKIP
        item.summary = "postgres 二进制不存在，跳过"
        return item
    rc, out, err = _run(["ldd", postgres], timeout=args.timeout)
    if rc != 0:
        item.status = STATUS_SKIP
        item.summary = f"ldd 执行失败（rc={rc}）: {err or out}"
        return item
    missing = [line.strip() for line in out.splitlines() if "not found" in line]
    if missing:
        item.status = STATUS_FAIL
        item.summary = f"{len(missing)} 个动态库无法解析"
        item.evidence = missing[:10]
        item.causes = ["LD_LIBRARY_PATH 未包含 compat/runtime 目录，或依赖库缺失"]
        item.fix_hint = "source env.sh 后重试；必要时用 ldd 逐项核对缺失库路径"
    else:
        item.summary = f"全部依赖可解析（{len(out.splitlines())} 行）"
    return item


def check_libpq_symlink(args):
    item = Item("libpq.symlink", "libpq.so.5 符号链接")
    link = os.path.join(args.runtime_libpq, "libpq.so.5")
    target = os.path.join(args.pg_bin, "..", "lib", "libpq.so.5.10")
    if not (os.path.lexists(link) and os.path.lexists(target)):
        item.status = STATUS_SKIP
        item.summary = f"路径不存在，跳过（runtime={args.runtime_libpq}）"
        return item
    resolved_link = os.path.realpath(link)
    resolved_target = os.path.realpath(target)
    if resolved_link == resolved_target:
        item.summary = "符号链接指向正确"
        item.evidence = [f"{link} -> {resolved_link}"]
    else:
        item.status = STATUS_FAIL
        item.summary = "libpq.so.5 未指向 pg/lib/libpq.so.5.10"
        item.evidence = [f"{link} -> {resolved_link}", f"期望 -> {resolved_target}"]
        item.causes = ["runtime/libpq 与 pg/lib 不一致，可能导致 PQconnectdbParallel 等 OpenTenBase 符号缺失"]
        item.fix_hint = f"ln -sf $(readlink -f {target}) {link}"
    return item


def check_ports(args):
    item = Item("port.check", "端口占用检查")
    try:
        listening = _ss_listening_ports()
    except OSError as exc:
        item.status = STATUS_ERROR
        item.summary = str(exc)
        return item
    for port in args.ports:
        if port in listening:
            if args.expect_running:
                item.evidence.append(f"端口 {port}: 监听中（预期）")
            else:
                item.evidence.append(f"端口 {port}: 已被占用")
                if item.status != STATUS_FAIL:
                    item.status = STATUS_WARN
        else:
            if args.expect_running:
                item.evidence.append(f"端口 {port}: 未监听")
                item.status = STATUS_FAIL
            else:
                item.evidence.append(f"端口 {port}: 空闲（可部署）")
    if args.expect_running and item.status == STATUS_FAIL:
        item.summary = "存在未监听的预期端口（节点可能未启动）"
        item.causes = ["节点未启动或已停止", "--ports 参数与实际部署不一致"]
        item.fix_hint = "用 pg_ctl/gtm_ctl status 核对各节点状态"
    elif not args.expect_running and item.status == STATUS_WARN:
        item.summary = "存在已占用端口（可能已有实例运行）"
        item.causes = ["已有 OpenTenBase 实例在运行", "端口被其他服务占用"]
    else:
        item.summary = "端口状态符合预期"
    return item


def check_dirs(args):
    item = Item("dir.check", "数据/日志目录")
    paths = args.data_dirs + [args.log_dir] + args.conf_files + args.pg_hba_files
    for p in paths:
        if os.path.isdir(p):
            item.evidence.append(f"存在: {p}")
        elif os.path.isfile(p):
            item.evidence.append(f"存在(文件): {p}")
        else:
            item.status = STATUS_FAIL
            item.evidence.append(f"缺失: {p}")
    if item.status == STATUS_FAIL:
        item.summary = "存在缺失的目录/文件"
        item.causes = ["安装不完整或 --data-dirs/--log-dir 参数与实际部署不一致"]
    else:
        item.summary = f"{len(paths)} 个路径均存在"
    return item


def check_disk(args):
    item = Item("disk.space", "磁盘空间")
    base = args.data_dirs[0] if args.data_dirs else "/"
    try:
        usage = shutil.disk_usage(base)
    except OSError as exc:
        item.status = STATUS_ERROR
        item.summary = f"无法获取磁盘信息: {exc}"
        return item
    free_gb = usage.free / 1024 ** 3
    item.evidence = [f"{base}: 可用 {free_gb:.1f} GB / 共 {usage.total / 1024**3:.1f} GB"]
    item.summary = f"可用 {free_gb:.1f} GB"
    if free_gb < args.disk_min_free_gb:
        item.status = STATUS_WARN
        item.summary = f"可用空间不足 {args.disk_min_free_gb} GB"
        item.causes = ["磁盘占用过高，可能影响 WAL/日志写入"]
        item.fix_hint = "清理日志与临时文件或扩容磁盘"
    return item


_GUC_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*=\s*(.+?)\s*(?:#.*)?$")


def check_duplicate_guc(args):
    item = Item("conf.duplicate_guc", "配置文件重复参数")
    if not args.conf_files:
        item.status = STATUS_SKIP
        item.summary = "未提供配置文件路径，跳过"
        return item
    dup_found = False
    for path in args.conf_files:
        seen = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    m = _GUC_RE.match(stripped)
                    if m:
                        seen.setdefault(m.group(1), []).append(lineno)
        except OSError as exc:
            item.status = STATUS_ERROR
            item.summary = f"读取 {path} 失败: {exc}"
            return item
        for name, linenos in seen.items():
            if len(linenos) > 1:
                dup_found = True
                item.evidence.append(f"{path}: {name} 定义于行 {linenos}（后者生效）")
    if dup_found:
        item.status = STATUS_WARN
        item.summary = "发现重复定义的参数"
        item.causes = ["同一参数在配置文件中出现多次，实际以后出现的值为准，易造成理解偏差"]
        item.fix_hint = "保留一处定义，删除其余（实验环境实测 DN1 曾同时定义 max_connections=100 与 =80）"
    else:
        item.summary = "未发现重复参数"
    return item


def check_pg_hba_trust(args):
    item = Item("auth.pg_hba_trust", "pg_hba 认证方式")
    if not args.pg_hba_files:
        item.status = STATUS_SKIP
        item.summary = "未提供 pg_hba.conf 路径，跳过"
        return item
    trust_lines = []
    for path in args.pg_hba_files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and stripped.split()[-1] == "trust":
                        trust_lines.append(f"{path}:{lineno}: {stripped}")
        except OSError as exc:
            item.status = STATUS_ERROR
            item.summary = f"读取 {path} 失败: {exc}"
            return item
    if trust_lines:
        item.status = STATUS_WARN
        item.summary = f"{len(trust_lines)} 条 trust 规则（密码校验未生效）"
        item.evidence = trust_lines[:5]
        item.causes = ["生产环境建议 md5/scram 认证，trust 只适用于本机隔离实验"]
        item.fix_hint = "评估是否需要收紧 pg_hba（不自动修改）"
    else:
        item.summary = "未发现 trust 规则"
    return item


def check_db_probe(args):
    item = Item("db.probe", "数据库连接探测（部署后验证）")
    if not args.conn:
        item.status = STATUS_SKIP
        item.summary = "未提供 --conn，跳过数据库探测（部署前检查不要求数据库已启动）"
        return item
    env = dict(os.environ)
    for kv in args.probe_env or []:
        k, _, v = kv.partition("=")
        env[k] = v
    psql_bin = os.path.join(args.pg_bin, "psql") if os.path.isfile(os.path.join(args.pg_bin, "psql")) else "psql"
    rc, out, err = _run(
        [psql_bin, args.conn, "-X", "-v", "ON_ERROR_STOP=1", "-Atc", "SELECT 1;"],
        env=env,
        timeout=args.timeout,
    )
    item.evidence = [f"exit_code={rc}"] + ([err[:300]] if err else [])
    if psql_bin != "psql":
        item.evidence.insert(1, f"psql 路径: {psql_bin}（--pg-bin）")
    if args.probe_env:
        shown = []
        for kv in args.probe_env:
            k, _, v = kv.partition("=")
            if k.upper() in ("PGPASSWORD", "PASSWORD"):
                v = "***"
            shown.append(f"{k}={v}")
        item.evidence.append(f"probe env 覆盖: {shown}")
    if rc == 0:
        item.summary = "连接成功（SELECT 1 = 1）"
    elif rc == 127:
        item.status = STATUS_FAIL
        # FileNotFoundError 文本为 "[Errno 2] No such file or directory: 'psql'"；
        # 动态库缺失文本为 "libxxx.so: cannot open shared object file: No such file or directory"，
        # 须以 [Errno 2] 精确区分。
        if "[Errno 2]" in err and "psql" in err:
            item.summary = "psql 未找到（PATH 不完整，未 source env.sh）"
            item.causes = ["psql 不在 PATH 中：未 source <安装目录>/env.sh，或 --pg-bin 未加入 PATH"]
            item.fix_hint = "source <安装目录>/env.sh 后再运行（实验环境为 /opt/opentenbase-lab/env.sh）"
        else:
            item.summary = "psql 启动失败（动态库缺失）"
            for sig in _match_signatures(err):
                item.causes.extend(sig["possible_causes"])
                item.fix_hint = "；".join(sig["next_steps"])
    elif "Connection refused" in err and not args.expect_running:
        item.status = STATUS_WARN
        item.summary = "数据库未运行（部署前检查属预期状态）"
    else:
        item.status = STATUS_FAIL
        item.summary = "连接失败"
        for sig in _match_signatures(err):
            item.causes.extend(sig["possible_causes"])
            if not item.fix_hint:
                item.fix_hint = "；".join(sig["next_steps"])
    if item.status == STATUS_FAIL and not item.causes:
        item.causes = ["连接参数错误或认证失败"]
    return item


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="otb_preflight.py",
        description="OpenTenBase 部署环境只读预检（无第三方依赖）",
    )

    def exit3(message):
        parser.print_usage(sys.stderr)
        print(f"otb_preflight.py: error: {message}", file=sys.stderr)
        sys.exit(3)

    parser.error = exit3
    parser.add_argument("--pg-bin", default=DEFAULT_PG_BIN)
    parser.add_argument("--runtime-libpq", default=DEFAULT_RUNTIME_LIBPQ)
    parser.add_argument("--compat-dirs", default=",".join(DEFAULT_COMPAT_DIRS),
                        help="逗号分隔（默认实验环境 compat 目录）")
    parser.add_argument("--data-dirs", default=",".join(DEFAULT_DATA_DIRS),
                        help="逗号分隔（默认实验环境数据目录）")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--ports", default=",".join(map(str, DEFAULT_PORTS)),
                        help="逗号分隔端口列表")
    parser.add_argument("--conf-files", default="", help="逗号分隔 postgresql.conf 路径")
    parser.add_argument("--pg-hba-files", default="", help="逗号分隔 pg_hba.conf 路径")
    parser.add_argument("--conn", default="", help="libpq 连接串（提供后追加 psql 探测）")
    parser.add_argument("--expect-running", action="store_true",
                        help="数据库应已运行：端口未监听/连接拒绝判 FAIL；默认视为部署前检查")
    parser.add_argument("--probe-env", action="append", default=None,
                        help="psql 探测环境覆盖（K=V，可多次），仅影响探测子进程")
    parser.add_argument("--disk-min-free-gb", type=float, default=1.0)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--strict", action="store_true",
                        help="WARN 级检查项也计入退出码 1（默认只有 FAIL 级才非零）")
    args = parser.parse_args(argv)

    args.compat_dirs = [d for d in args.compat_dirs.split(",") if d]
    args.data_dirs = [d for d in args.data_dirs.split(",") if d]
    try:
        ports = [int(p) for p in args.ports.split(",") if p]
    except ValueError:
        print(f"otb_preflight.py: error: 端口参数格式错误（需为整数，逗号分隔）: {args.ports}", file=sys.stderr)
        sys.exit(3)
    for p in ports:
        if not 1 <= p <= 65535:
            print(f"otb_preflight.py: error: 端口超出 1-65535 范围: {p}", file=sys.stderr)
            sys.exit(3)
    args.ports = ports
    args.conf_files = [p for p in args.conf_files.split(",") if p]
    args.pg_hba_files = [p for p in args.pg_hba_files.split(",") if p]

    items = [
        check_env_ld_library_path(args),
        check_binaries(args),
        check_ldd(args),
        check_libpq_symlink(args),
        check_ports(args),
        check_dirs(args),
        check_disk(args),
        check_duplicate_guc(args),
        check_pg_hba_trust(args),
        check_db_probe(args),
    ]
    print(f"otb_preflight 检查 {len(items)} 项（只读）  conn={_redact_conninfo(args.conn) or '(未提供)'}")
    for it in items:
        print(it.line())
    statuses = [it.status for it in items]
    if STATUS_ERROR in statuses:
        exit_code = 2
    elif STATUS_FAIL in statuses:
        exit_code = 1
    elif args.strict and STATUS_WARN in statuses:
        exit_code = 1
    else:
        exit_code = 0
    print(f"exit_code={exit_code}（0 无 FAIL（WARN 允许）/ 1 存在 FAIL（--strict 下 WARN 计 1）/ 2 执行失败或不完整 / 3 参数错误）")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
