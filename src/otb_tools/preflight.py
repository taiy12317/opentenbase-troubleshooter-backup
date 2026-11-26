"""preflight 子命令：部署/启动前环境检查（默认不要求数据库已启动）。

默认值对应课程实验环境的部署方式（/opt/opentenbase-lab + env.sh 布局），
均可通过 CLI 参数覆盖。检查只读，不修改任何环境。

职责边界：只做部署前/启动前的环境检查（二进制、动态库、环境变量、端口、
目录、磁盘、配置文件、认证方式、启动失败签名）。提供 --conn 时追加部署后
验证（连接探测），运行期巡检（节点组/2PC/会话水位/日志关联）由 inspect 负责。
"""

from __future__ import annotations

import ipaddress
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field

from . import net, rules
from .models import Item, Report, Status
from .db import redact_conninfo

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


@dataclass
class PreflightConfig:
    pg_bin: str = DEFAULT_PG_BIN
    runtime_libpq: str = DEFAULT_RUNTIME_LIBPQ
    compat_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_COMPAT_DIRS))
    data_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_DATA_DIRS))
    log_dir: str = DEFAULT_LOG_DIR
    ports: list[int] = field(default_factory=lambda: list(DEFAULT_PORTS))
    conf_files: list[str] = field(default_factory=list)
    pg_hba_files: list[str] = field(default_factory=list)
    conninfo: str = ""
    expect_running: bool = False
    probe_env: dict = field(default_factory=dict)
    disk_min_free_gb: float = 1.0
    timeout: int = 10


def _run(cmd: list[str], env: dict | None = None, timeout: int = 10) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except OSError as exc:
        # 无执行权限等场景：仅记录证据，不透传为工具退出码
        return 126, "", str(exc)
    except subprocess.TimeoutExpired:
        # 不回显命令本身：db.probe 的命令行含完整连接串（可能带密码），回显即泄漏
        return 124, "", f"command timed out after {timeout}s"


def _attached_signatures(text: str) -> list[dict]:
    return rules.match_signatures(text)


def _check_env_ld_library_path(cfg: PreflightConfig) -> Item:
    item = Item(id="env.ld_library_path", name="LD_LIBRARY_PATH 环境变量", status=Status.PASS)
    val = os.environ.get("LD_LIBRARY_PATH", "")
    if not val:
        item.status = Status.WARN
        item.summary = "LD_LIBRARY_PATH 未设置"
        item.causes = ["运行 psql/postgres 可能报 libreadline.so.7 / libssl.so.1.1 缺失（exit 127）"]
        item.fix_hint = "source <安装目录>/env.sh 后再运行（实验环境为 /opt/opentenbase-lab/env.sh）"
        return item
    item.evidence = [f"LD_LIBRARY_PATH={val}"]
    item.summary = f"已设置（{len(val.split(':'))} 个目录）"
    for d in cfg.compat_dirs:
        if d and d not in val:
            item.status = Status.WARN
            item.summary = f"缺少建议目录 {d}"
            item.facts = ["实测缺失该目录时会报 libreadline.so.7 无法加载（exit 127）"]
    return item


def _check_binaries(cfg: PreflightConfig) -> Item:
    item = Item(id="bin.tools", name="数据库工具二进制", status=Status.PASS)
    for tool in ["postgres", "psql", "gtm_ctl", "pg_ctl"]:
        p = os.path.join(cfg.pg_bin, tool)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            item.evidence.append(f"存在且可执行: {p}")
        else:
            item.status = Status.FAIL
            item.evidence.append(f"缺失或不可执行: {p}")
    item.summary = "工具链完整" if item.status == Status.PASS else "存在缺失的工具"
    if item.status == Status.FAIL:
        item.causes = [f"{cfg.pg_bin} 下缺少对应二进制，安装不完整或 PG_BIN 参数错误"]
        item.fix_hint = f"核对 --pg-bin（当前 {cfg.pg_bin}）或补装 OpenTenBase"
    return item


def _check_ldd(cfg: PreflightConfig) -> Item:
    item = Item(id="dynlib.ldd", name="postgres 动态库依赖", status=Status.PASS)
    postgres = os.path.join(cfg.pg_bin, "postgres")
    if not os.path.isfile(postgres):
        item.status = Status.SKIP
        item.summary = "postgres 二进制不存在，跳过"
        return item
    rc, out, err = _run(["ldd", postgres], timeout=cfg.timeout)
    if rc != 0:
        item.status = Status.SKIP
        item.summary = f"ldd 执行失败（rc={rc}）: {err or out}"
        return item
    missing = [line.strip() for line in out.splitlines() if "not found" in line]
    if missing:
        item.status = Status.FAIL
        item.summary = f"{len(missing)} 个动态库无法解析"
        item.evidence = missing[:10]
        item.causes = ["LD_LIBRARY_PATH 未包含 compat/runtime 目录，或依赖库缺失"]
        item.fix_hint = "source env.sh 后重试；必要时用 ldd 逐项核对缺失库路径"
        item.facts = ["实测缺失 libreadline.so.7 时 psql 启动报错且退出码 127"]
    else:
        item.summary = f"全部依赖可解析（{len(out.splitlines())} 行）"
    return item


def _check_libpq_symlink(cfg: PreflightConfig) -> Item:
    item = Item(id="libpq.symlink", name="libpq.so.5 符号链接", status=Status.PASS)
    link = os.path.join(cfg.runtime_libpq, "libpq.so.5")
    target = os.path.join(cfg.pg_bin, "..", "lib", "libpq.so.5.10")
    if not (os.path.lexists(link) and os.path.lexists(target)):
        item.status = Status.SKIP
        item.summary = f"路径不存在，跳过（runtime={cfg.runtime_libpq}）"
        return item
    resolved_link = os.path.realpath(link)
    resolved_target = os.path.realpath(target)
    if resolved_link == resolved_target:
        item.summary = "符号链接指向正确"
        item.evidence = [f"{link} -> {resolved_link}"]
    else:
        item.status = Status.FAIL
        item.summary = "libpq.so.5 未指向 pg/lib/libpq.so.5.10"
        item.evidence = [f"{link} -> {resolved_link}", f"期望 -> {resolved_target}"]
        item.causes = ["runtime/libpq 与 pg/lib 不一致，可能导致 PQconnectdbParallel 等 OpenTenBase 符号缺失"]
        item.fix_hint = f"ln -sf $(readlink -f {target}) {link}"
        item.facts = ["start_lab.sh 首段即校验该链接（实测样本）"]
    return item


def _check_ports(cfg: PreflightConfig) -> Item:
    item = Item(id="port.check", name="端口占用检查", status=Status.PASS)
    try:
        listening = net.ss_listening_ports()
    except OSError as exc:
        item.status = Status.ERROR
        item.summary = str(exc)
        return item
    item.source = ["ss -ltn"]
    for port in cfg.ports:
        if port in listening:
            if cfg.expect_running:
                item.evidence.append(f"端口 {port}: 监听中（预期）")
            else:
                item.evidence.append(f"端口 {port}: 已被占用")
                if item.status != Status.FAIL:
                    item.status = Status.WARN
        else:
            if cfg.expect_running:
                item.evidence.append(f"端口 {port}: 未监听")
                item.status = Status.FAIL
            else:
                item.evidence.append(f"端口 {port}: 空闲（可部署）")
    if cfg.expect_running and item.status == Status.FAIL:
        item.summary = "存在未监听的预期端口（节点可能未启动）"
        item.causes = ["节点未启动或已停止", "--ports 参数与实际部署不一致"]
        item.fix_hint = "用 pg_ctl/gtm_ctl status 核对各节点状态"
    elif not cfg.expect_running and item.status == Status.WARN:
        item.summary = "存在已占用端口（可能已有实例运行）"
        item.causes = ["已有 OpenTenBase 实例在运行", "端口被其他服务占用"]
    else:
        item.summary = "端口状态符合预期"
    return item


def _check_dirs(cfg: PreflightConfig) -> Item:
    item = Item(id="dir.check", name="数据/日志目录", status=Status.PASS)
    paths = cfg.data_dirs + [cfg.log_dir] + list(cfg.conf_files) + list(cfg.pg_hba_files)
    for p in paths:
        if os.path.isdir(p):
            item.evidence.append(f"存在: {p}")
        elif os.path.isfile(p):
            item.evidence.append(f"存在(文件): {p}")
        else:
            item.status = Status.FAIL
            item.evidence.append(f"缺失: {p}")
    if item.status == Status.FAIL:
        item.summary = "存在缺失的目录/文件"
        item.causes = ["安装不完整或 --data-dirs/--log-dir 参数与实际部署不一致"]
    else:
        item.summary = f"{len(paths)} 个路径均存在"
    return item


def _check_disk(cfg: PreflightConfig) -> Item:
    item = Item(id="disk.space", name="磁盘空间", status=Status.PASS)
    base = cfg.data_dirs[0] if cfg.data_dirs else "/"
    try:
        usage = shutil.disk_usage(base)
    except OSError as exc:
        item.status = Status.ERROR
        item.summary = f"无法获取磁盘信息: {exc}"
        return item
    free_gb = usage.free / 1024**3
    item.evidence = [f"{base}: 可用 {free_gb:.1f} GB / 共 {usage.total / 1024**3:.1f} GB"]
    item.summary = f"可用 {free_gb:.1f} GB"
    if free_gb < cfg.disk_min_free_gb:
        item.status = Status.WARN
        item.summary = f"可用空间不足 {cfg.disk_min_free_gb} GB"
        item.causes = ["磁盘占用过高，可能影响 WAL/日志写入"]
        item.fix_hint = "清理日志与临时文件或扩容磁盘"
    return item


def _check_conf_readable(cfg: PreflightConfig) -> Item:
    item = Item(id="conf.readable", name="配置文件可读性", status=Status.PASS)
    for p in cfg.conf_files:
        if not os.path.isfile(p):
            item.status = Status.SKIP
            item.summary = f"未提供配置文件路径，跳过"
            return item
        if os.access(p, os.R_OK):
            item.evidence.append(f"可读: {p}")
        else:
            item.status = Status.FAIL
            item.evidence.append(f"不可读: {p} (mode {oct(os.stat(p).st_mode & 0o777)})")
    if item.status == Status.FAIL:
        item.summary = "配置文件不可读"
        item.causes = ["配置检查需以 root 或数据库属主身份运行（实验环境文件为 opentenbase:600）"]
        item.fix_hint = "以具备权限的运行身份执行本工具"
    else:
        item.summary = f"{len(cfg.conf_files)} 个配置文件可读"
    return item


_GUC_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*=\s*(.+?)\s*(?:#.*)?$")


def _conf_entries(path: str) -> list[tuple[int, str, str]]:
    """返回 (行号, 参数名, 值) 列表。仅解析简单 `name = value` 行。"""
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _GUC_RE.match(stripped)
            if m:
                out.append((lineno, m.group(1), m.group(2)))
    return out


def _check_duplicate_guc(cfg: PreflightConfig) -> Item:
    item = Item(id="conf.duplicate_guc", name="配置文件重复参数", status=Status.PASS)
    if not cfg.conf_files:
        item.status = Status.SKIP
        item.summary = "未提供配置文件路径，跳过"
        return item
    dup_found = False
    for path in cfg.conf_files:
        try:
            entries = _conf_entries(path)
        except OSError as exc:
            item.status = Status.ERROR
            item.summary = f"读取 {path} 失败: {exc}"
            return item
        seen: dict[str, list[int]] = {}
        for lineno, name, value in entries:
            seen.setdefault(name, []).append(lineno)
        for name, linenos in seen.items():
            if len(linenos) > 1:
                dup_found = True
                item.evidence.append(f"{path}: {name} 定义于行 {linenos}（后者生效）")
    if dup_found:
        item.status = Status.WARN
        item.summary = "发现重复定义的参数"
        item.causes = ["同一参数在配置文件中出现多次，实际以后出现的值为准，易造成理解偏差"]
        item.fix_hint = "保留一处定义，删除其余（实验环境实测 DN1 曾同时定义 max_connections=100 与 =80）"
    else:
        item.summary = "未发现重复参数"
    return item


def _check_pg_hba_trust(cfg: PreflightConfig) -> Item:
    item = Item(id="auth.pg_hba_trust", name="pg_hba 认证方式", status=Status.PASS)
    if not cfg.pg_hba_files:
        item.status = Status.SKIP
        item.summary = "未提供 pg_hba.conf 路径，跳过"
        return item
    trust_lines = []
    for path in cfg.pg_hba_files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    stripped = line.strip()
                    if _pg_hba_auth_method(stripped) == "trust":
                        trust_lines.append(f"{path}:{lineno}: {stripped}")
        except OSError as exc:
            item.status = Status.ERROR
            item.summary = f"读取 {path} 失败: {exc}"
            return item
    if trust_lines:
        item.status = Status.WARN
        item.summary = f"{len(trust_lines)} 条 trust 规则（密码校验未生效）"
        item.evidence = trust_lines[:5]
        item.facts = ["实测本集群 trust 配置下，错误密码仍可连接并执行查询"]
        item.causes = ["生产环境建议 md5/scram 认证，trust 只适用于本机隔离实验"]
        item.fix_hint = "评估是否需要收紧 pg_hba（不自动修改）"
    else:
        item.summary = "未发现 trust 规则"
    return item


def _pg_hba_auth_method(line: str) -> str | None:
    """Extract the pg_hba authentication field without treating comments/options as it.

    pg_hba records place the method after three fields for ``local`` and after
    four fields for ``host`` variants. The legacy host/netmask form adds one
    address field. shlex handles quoted fields and strips unquoted ``#`` comments.
    This is intentionally a narrow field reader, not a full pg_hba validator.
    """
    lexer = shlex.shlex(line, posix=True)
    lexer.quotes = '"'
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        fields = list(lexer)
    except ValueError:
        return None
    if not fields:
        return None
    record = fields[0].lower()
    if record == "local":
        return fields[3].lower() if len(fields) > 3 else None
    if record not in {"host", "hostssl", "hostnossl", "hostgssenc", "hostnogssenc"}:
        return None
    if len(fields) < 5:
        return None
    # A separate IPv4/IPv6 netmask follows the address in the legacy syntax.
    if len(fields) > 5:
        try:
            ipaddress.ip_address(fields[4])
        except ValueError:
            pass
        else:
            return fields[5].lower()
    return fields[4].lower()


def _check_db_probe(cfg: PreflightConfig) -> Item:
    item = Item(id="db.probe", name="数据库连接探测（部署后验证）", status=Status.PASS)
    if not cfg.conninfo:
        item.status = Status.SKIP
        item.summary = "未提供 --conn，跳过数据库探测（部署前检查不要求数据库已启动）"
        return item
    item.facts = ["提供 --conn 即进入部署后验证模式，仅做连接探测；运行期巡检（节点组/2PC/会话水位/日志关联）由 inspect 负责"]
    env = dict(os.environ)
    env.update(cfg.probe_env)
    # 优先使用 --pg-bin 下的 psql（与部署一致），不存在时回退 PATH
    psql_bin = os.path.join(cfg.pg_bin, "psql") if os.path.isfile(os.path.join(cfg.pg_bin, "psql")) else "psql"
    rc, out, err = _run(
        [psql_bin, f"--dbname={cfg.conninfo}", "-X", "-v", "ON_ERROR_STOP=1", "-Atc", "SELECT 1;"],
        env=env,
        timeout=cfg.timeout,
    )
    item.source = [f"{psql_bin} {redact_conninfo(cfg.conninfo)}"]
    item.evidence = [f"exit_code={rc}"] + ([err[:300]] if err else [])
    if psql_bin != "psql":
        item.evidence.append(f"psql 路径: {psql_bin}（--pg-bin）")
    else:
        item.evidence.append("psql 路径: PATH 回退（--pg-bin 下无 psql）")
    if cfg.probe_env:
        # probe_env 可能包含密码类变量（如 PGPASSWORD），显示时脱敏
        shown_env = {
            k: ("***" if k.upper() in {"PGPASSWORD", "PASSWORD"} else v)
            for k, v in cfg.probe_env.items()
        }
        item.evidence.append(f"probe env 覆盖: {shown_env}")
    if rc == 0 and out.strip() == "1":
        item.summary = "连接成功（SELECT 1 = 1）"
    elif rc == 0:
        item.status = Status.FAIL
        item.summary = "psql 未返回预期探测结果"
        item.causes = ["连接探测未得到单一结果 1，不能确认数据库连接成功"]
        item.fix_hint = "核对 psql 输出与连接参数；连接串作为 --dbname 值传入"
    elif rc == 126:
        item.status = Status.FAIL
        item.summary = "psql 执行失败（二进制存在但不可执行）"
        item.causes = ["psql 权限不足（无执行位）或架构不匹配"]
        item.fix_hint = "核对 psql 权限与架构，或用 --pg-bin 指向可执行的版本"
    elif rc == 127:
        item.status = Status.FAIL
        # FileNotFoundError 的典型文本为 "[Errno 2] No such file or directory: 'psql'"；
        # 动态库缺失的文本是 "libxxx.so: cannot open shared object file: No such file or directory"，
        # 须以 [Errno 2] 精确区分，不能用子串 "No such file or directory"。
        if "[Errno 2]" in err and "psql" in err:
            item.summary = "psql 未找到（PATH 不完整，未 source env.sh）"
            item.causes = ["psql 不在 PATH 中：未 source <安装目录>/env.sh，或 --pg-bin 未加入 PATH"]
            item.fix_hint = "source <安装目录>/env.sh 后再运行（实验环境为 /opt/opentenbase-lab/env.sh）"
        else:
            item.summary = "psql 启动失败（动态库缺失）"
            for sig in _attached_signatures(err):
                item.causes.extend(sig["possible_causes"])
                item.fix_hint = "；".join(sig["next_steps"])
    elif "Connection refused" in err and not cfg.expect_running:
        item.status = Status.WARN
        item.summary = "数据库未运行（部署前检查属预期状态）"
    else:
        item.status = Status.FAIL
        item.summary = "连接失败"
        for sig in _attached_signatures(err):
            item.causes.extend(sig["possible_causes"])
            if not item.fix_hint:
                item.fix_hint = "；".join(sig["next_steps"])
    if item.status == Status.FAIL and not item.causes:
        item.causes = ["连接参数错误或认证失败"]
    return item


def run(cfg: PreflightConfig, strict: bool = False) -> Report:
    report = Report(command="preflight", strict=strict)
    report.items = [
        _check_env_ld_library_path(cfg),
        _check_binaries(cfg),
        _check_ldd(cfg),
        _check_libpq_symlink(cfg),
        _check_ports(cfg),
        _check_dirs(cfg),
        _check_disk(cfg),
        _check_conf_readable(cfg),
        _check_duplicate_guc(cfg),
        _check_pg_hba_trust(cfg),
        _check_db_probe(cfg),
    ]
    return report
