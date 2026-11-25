"""inspect 子命令：运行期集群巡检（只读）。

原则：
  - 节点连接失败只影响该节点相关检查，其余检查继续；
  - 参数差异不等于配置错误（WARN 而非 FAIL）；
  - 存在 prepared transaction 不等于事务异常（WARN 带说明）；
  - 历史 ERROR/FATAL 不等于当前故障（标注时间与窗口）；
  - 证据不足时明确说明，不编造根因。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import timedelta

from . import log_parser
from .db import connect, redact_conninfo, run_sql
from .models import Correlation, Item, Report, Status

# 不含用户名/密码的默认连接串：凭据必须由调用方通过 --conn 显式提供
DEFAULT_CONNINFO = "host=127.0.0.1 port=15001"
DEFAULT_LOG_DIR = "/data/opentenbase-lab/logs"
DEFAULT_LOG_FILES = {"cn": "cn.log", "dn1": "dn1.log", "dn2": "dn2.log", "gtm": "gtm.log"}
COMPARABLE_PARAMS = ["max_connections", "shared_buffers", "log_destination", "logging_collector", "max_prepared_transactions"]
SKEW_MAX_ROWS_DEFAULT = 100_000
SKEW_FAIL_MIN_ROWS_DEFAULT = 10_000
SKEW_WARN_RATIO = 2.0


@dataclass
class InspectorConfig:
    conninfo: str = DEFAULT_CONNINFO
    ports: list[int] = field(default_factory=list)
    log_dir: str = DEFAULT_LOG_DIR
    log_files: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LOG_FILES))
    log_window_hours: float = 24.0
    watermark_threshold: float = 0.8
    timeout: int = 5
    node_roles: dict[str, str] = field(default_factory=dict)
    skew_max_rows: int = SKEW_MAX_ROWS_DEFAULT
    skew_fail_min_rows: int = SKEW_FAIL_MIN_ROWS_DEFAULT
    skew_tables: list[str] = field(default_factory=list)


def _qstr(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _qident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _node_role(node_name: str, node_type: str) -> str:
    return {"G": "gtm", "C": "coordinator", "D": "datanode"}.get(node_type, "unknown")


def _roles_from_log_files(cfg: InspectorConfig) -> dict[str, str]:
    """数据库不可用（无 pgxc_node 数据）时，从 --log-files 的显式节点键推断角色。

    键由用户显式命名（如 cn=cn.log），按前缀归类；无法归类的不参与关联。
    """
    roles: dict[str, str] = {}
    for node in cfg.log_files:
        low = node.lower()
        if low.startswith("cn"):
            roles[node] = "coordinator"
        elif low.startswith("dn"):
            roles[node] = "datanode"
        elif low.startswith("gtm"):
            roles[node] = "gtm"
    return roles


def _conninfo_for_node(conninfo: str, host: str | None = None, port: int | None = None) -> str:
    """基于原连接串构造目标节点连接串，覆盖 host/port，其余参数（user/password/dbname/
    sslmode/options 等）保留。用 psycopg2 的 make_dsn 解析并重建：正确处理带引号/空格的
    值（key=value 形式）与百分号编码（URI 形式），避免正则手工解析导致的截断与编码错误。
    host 为空时保持原 host（注册信息缺失时的降级，由调用方在证据中说明）。
    """
    import psycopg2

    kwargs: dict = {}
    if port is not None:
        kwargs["port"] = str(port)
    if host:
        kwargs["host"] = host
    return psycopg2.extensions.make_dsn(conninfo, **kwargs)


def _conninfo_for_port(conninfo: str, port: int) -> str:
    return _conninfo_for_node(conninfo, port=port)


def _run(cmd: list[str], timeout: int = 5) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"command timed out after {timeout}s"


def _ss_listening_ports() -> set[int]:
    rc, out, err = _run(["ss", "-ltn"], timeout=5)
    if rc != 0:
        raise OSError(f"ss 不可用: {err or out}")
    ports: set[int] = set()
    for line in out.splitlines():
        m = re.search(r":(\d+)\s", line)
        if m:
            ports.add(int(m.group(1)))
    return ports


def _db_error_item(name: str, exc: Exception) -> Item:
    item = Item(id=name, name=name, status=Status.ERROR)
    item.summary = str(exc)[:200]
    return item


def _query_nodes(conn) -> tuple[list[dict], str]:
    rows, cols = run_sql(
        conn,
        "SELECT node_name, node_type, node_port, node_host, nodeis_primary, node_id "
        "FROM pgxc_node ORDER BY node_id;",
    )
    nodes = []
    for r in rows:
        d = dict(zip(cols, r))
        nodes.append(d)
    return nodes, ""


def run(cfg: InspectorConfig, strict: bool = False) -> Report:
    report = Report(command="inspect", strict=strict)

    conn = None
    conn_item = Item(id="db.connect", name="CN 连接", status=Status.PASS)
    try:
        conn = connect(cfg.conninfo, timeout=cfg.timeout)
    except ImportError as exc:
        conn_item.status = Status.ERROR
        conn_item.summary = "psycopg2 未安装（pip install 'otb-troubleshooting-toolkit[db]'），数据库相关检查不可用"
        report.items.append(conn_item)
        _append_node_level_checks(report, cfg)
        _append_log_checks(report, cfg, [])
        return report
    except Exception as exc:
        conn_item.status = Status.ERROR
        conn_item.summary = f"连接失败: {str(exc)[:150]}"
        conn_item.source = [f"conninfo={redact_conninfo(cfg.conninfo)}"]
        conn_item.causes = ["CN 未运行或连接参数错误（含 user/password 未提供或错误）"]
        conn_item.fix_hint = "用 --conn 提供完整连接串并核对 CN 端口监听；数据库不可用不影响端口/进程/日志检查"
        report.items.append(conn_item)
        _append_node_level_checks(report, cfg)
        _append_log_checks(report, cfg, [])
        return report

    conn_item.summary = "连接成功（只读会话）"
    conn_item.source = [f"conninfo={redact_conninfo(cfg.conninfo)}"]
    report.items.append(conn_item)

    # pgxc_node 注册信息
    reg_item = Item(id="pgxc.node_registration", name="节点注册（pgxc_node）", status=Status.PASS)
    nodes: list[dict] = []
    try:
        nodes, _ = _query_nodes(conn)
        reg_item.evidence = [
            f"{n['node_name']} type={n['node_type']} port={n['node_port']} host={n['node_host']} primary={n['nodeis_primary']}"
            for n in nodes
        ]
        reg_item.summary = f"{len(nodes)} 个节点已注册"
        reg_item.source = ["pgxc_node"]
    except Exception as exc:
        reg_item.status = Status.ERROR
        reg_item.summary = str(exc)[:200]
    report.items.append(reg_item)

    # 节点组
    grp_item = Item(id="pgxc.group", name="节点组（pgxc_group）", status=Status.PASS)
    try:
        rows, cols = run_sql(conn, "SELECT * FROM pgxc_group;")
        grp_item.evidence = [f"{dict(zip(cols, r))}" for r in rows]
        grp_item.summary = f"{len(rows)} 个节点组" if rows else "无节点组（可能初始化不完整）"
        grp_item.source = ["pgxc_group"]
        if not rows:
            grp_item.status = Status.FAIL
            grp_item.causes = ["集群初始化不完整或元数据被修改（实测日志可见 'default group not defined'）"]
    except Exception as exc:
        grp_item.status = Status.ERROR
        grp_item.summary = str(exc)[:200]
    report.items.append(grp_item)

    # 注册信息 vs 实际监听
    port_item = Item(id="pgxc.reg_vs_ports", name="注册信息与实际监听一致性", status=Status.PASS)
    try:
        listening = _ss_listening_ports()
        port_item.source = ["pgxc_node", "ss -ltn"]
        for n in nodes:
            try:
                port = int(n["node_port"])
            except (TypeError, ValueError):
                port_item.status = Status.FAIL
                port_item.evidence.append(f"{n['node_name']}: 注册端口无效（{n.get('node_port')!r}），无法核对监听")
                continue
            if port in listening:
                port_item.evidence.append(f"{n['node_name']}: 端口 {port} 监听中")
            else:
                port_item.status = Status.FAIL
                port_item.evidence.append(f"{n['node_name']}: 注册端口 {port} 未监听")
        if port_item.status == Status.FAIL:
            port_item.summary = "存在注册但未监听的节点（节点可能宕机）"
            port_item.causes = ["节点进程未运行", "节点崩溃或未随集群启动"]
            port_item.fix_hint = "用 pg_ctl/gtm_ctl status 核对；检查 /data 下节点日志"
        else:
            port_item.summary = "注册端口均处于监听状态"
    except OSError as exc:
        port_item.status = Status.ERROR
        port_item.summary = str(exc)
    report.items.append(port_item)

    # 进程
    report.items.append(_processes_item())

    # 直连各 DN（用 pgxc_node 注册的 host/port，不沿用 CN 的 host）
    direct_item = Item(id="conn.direct_dn", name="CN→DN 直连可达性", status=Status.PASS)
    for n in nodes:
        if n["node_type"] != "D":
            continue
        label = f"{n['node_name']} ({n.get('node_host') or '(无注册host)'}:{n.get('node_port') or '(无注册port)'})"
        if not n.get("node_port"):
            direct_item.status = Status.FAIL
            direct_item.evidence.append(f"{label}: 注册信息缺少端口，无法构造直连")
            continue
        dn_conn = None
        try:
            dn_conn = connect(
                _conninfo_for_node(cfg.conninfo, host=n.get("node_host") or "", port=int(n["node_port"])),
                timeout=cfg.timeout,
            )
            rows, _ = run_sql(dn_conn, "SELECT current_setting('port');")
            direct_item.evidence.append(f"{label}: 直连成功 port={rows[0][0]}")
        except Exception as exc:
            direct_item.status = Status.FAIL
            direct_item.evidence.append(f"{label}: 直连失败 {str(exc)[:120]}")
        finally:
            if dn_conn is not None:
                try:
                    dn_conn.close()
                except Exception:
                    pass
    if direct_item.status == Status.FAIL:
        direct_item.summary = "部分 DN 直连失败（其余检查继续）"
        direct_item.causes = ["DN 不可达或未运行"]
    elif direct_item.evidence:
        direct_item.summary = f"{len(direct_item.evidence)} 个 DN 直连成功"
    else:
        direct_item.status = Status.SKIP
        direct_item.summary = "未发现 D 类型节点"
    report.items.append(direct_item)

    # 生效配置对比
    cfg_item = Item(id="cfg.effective", name="跨节点生效配置对比", status=Status.PASS)
    effective: dict[str, dict] = {}
    targets = []
    for n in nodes:
        if n["node_type"] == "C":
            targets.append((n["node_name"], cfg.conninfo))
        elif n["node_type"] == "D":
            if n.get("node_port"):
                targets.append((
                    n["node_name"],
                    _conninfo_for_node(cfg.conninfo, host=n.get("node_host") or "", port=int(n["node_port"])),
                ))
            else:
                effective[n["node_name"]] = {"_error": "注册信息缺少端口，无法构造连接"}
    seen_names = set()
    for name, conninfo in targets:
        if name in seen_names:
            continue
        seen_names.add(name)
        try:
            c = connect(conninfo, timeout=cfg.timeout)
            vals = {}
            for p in COMPARABLE_PARAMS:
                try:
                    rows, _ = run_sql(c, f"SELECT current_setting('{p}');")
                    vals[p] = rows[0][0]
                except Exception:
                    vals[p] = None
            c.close()
            effective[name] = vals
        except Exception as exc:
            effective[name] = {"_error": str(exc)[:120]}
    for name, vals in effective.items():
        if "_error" in vals:
            cfg_item.evidence.append(f"{name}: 采集失败 {vals['_error']}")
        else:
            cfg_item.evidence.append(f"{name}: " + " ".join(f"{k}={v}" for k, v in vals.items() if v is not None))
    # 状态聚合优先级：采集失败（ERROR）不被参数差异（WARN）覆盖；差异事实基于
    # 采集成功的节点记录，并注明采集不完整。
    diffs: list[tuple[str, list]] = []
    for param in COMPARABLE_PARAMS:
        values = {v.get(param) for v in effective.values() if isinstance(v.get(param), str)}
        if len(values) > 1:
            diffs.append((param, sorted(values)))
    errors = [name for name, vals in effective.items() if "_error" in vals]
    if diffs:
        collected = len(effective) - len(errors)
        cfg_item.facts.append(f"参数 {', '.join(p for p, _ in diffs)} 在已采集节点间取值不同"
                              + (f"（仅 {collected}/{len(effective)} 个节点采集成功，结论不完整）" if errors else ""))
    if errors:
        cfg_item.status = Status.ERROR
        if len(errors) == len(effective) and effective:
            cfg_item.summary = "所有节点配置采集失败"
        else:
            cfg_item.summary = "部分节点配置采集失败（其余检查继续）"
        cfg_item.causes = ["连接失败/权限不足/查询超时；采集失败不等于配置差异，二者分别记录"]
    elif diffs:
        cfg_item.status = Status.WARN
        cfg_item.summary = "节点间存在参数差异"
        cfg_item.causes = ["参数差异不一定表示配置错误（如 max_connections 可刻意不同），需结合部署意图判断"]
    else:
        cfg_item.summary = "对比参数取值一致"
    report.items.append(cfg_item)

    # 会话汇总
    sess_item = Item(id="sessions.summary", name="会话与异常会话", status=Status.PASS)
    try:
        rows, cols = run_sql(
            conn,
            "SELECT nodename, state, count(*) FROM pg_stat_cluster_activity "
            "WHERE backend_type = 'client backend' GROUP BY nodename, state ORDER BY 1, 2;",
        )
        sess_item.evidence = [f"{r[0] or '(本节点)'} {r[1]}: {r[2]}" for r in rows]
        non_idle = sum(r[2] for r in rows if r[1] != "idle")
        sess_item.summary = f"非 idle 会话 {non_idle} 个"
        sess_item.source = ["pg_stat_cluster_activity"]
        if non_idle > 0:
            rows2, _ = run_sql(
                conn,
                "SELECT nodename, pid, state, wait_event_type, wait_event, "
                "extract(epoch from (now() - query_start))::int AS running_s "
                "FROM pg_stat_cluster_activity "
                "WHERE backend_type = 'client backend' AND state <> 'idle' ORDER BY running_s DESC LIMIT 10;",
            )
            sess_item.evidence += [f"运行中: {dict(zip(['nodename','pid','state','wait_type','wait','running_s'], r))}" for r in rows2]
    except Exception as exc:
        sess_item.status = Status.ERROR
        sess_item.summary = str(exc)[:200]
    report.items.append(sess_item)

    # 连接水位
    wm_item = Item(id="sessions.watermark", name="连接水位", status=Status.PASS)
    try:
        cn_name = next((n["node_name"] for n in nodes if n["node_type"] == "C"), "(cn)")
        counts: dict[str, int] = {}
        rows, _ = run_sql(
            conn,
            "SELECT nodename, count(*) FROM pg_stat_cluster_activity "
            "WHERE backend_type = 'client backend' GROUP BY nodename;",
        )
        for r in rows:
            counts[r[0] or cn_name] = r[1]
        for name, maxv in effective.items():
            if isinstance(maxv.get("max_connections"), str):
                cnt = counts.get(name)
                if cnt is None:
                    continue
                ratio = cnt / int(maxv["max_connections"])
                wm_item.evidence.append(f"{name}: {cnt}/{maxv['max_connections']} ({ratio:.0%})")
                if ratio >= cfg.watermark_threshold:
                    wm_item.status = Status.WARN
        if wm_item.status == Status.WARN:
            wm_item.summary = f"水位达到阈值 {cfg.watermark_threshold:.0%}"
            wm_item.causes = ["连接数接近上限，新连接可能被拒绝（53300）"]
            wm_item.fix_hint = "排查空闲连接与连接泄漏；如确认需要可评估调大 max_connections（需重启）"
        else:
            wm_item.summary = "水位正常"
    except Exception as exc:
        wm_item.status = Status.ERROR
        wm_item.summary = str(exc)[:200]
    report.items.append(wm_item)

    # 2PC
    twopc_item = Item(id="2pc.count", name="残留 prepared 事务", status=Status.PASS)
    try:
        rows, cols = run_sql(conn, "SELECT gid, prepared, owner FROM pg_prepared_xacts;")
        twopc_item.summary = f"{len(rows)} 个 prepared 事务"
        twopc_item.evidence = [f"{dict(zip(cols, r))}" for r in rows[:10]]
        twopc_item.source = ["pg_prepared_xacts"]
        if rows:
            twopc_item.status = Status.WARN
            twopc_item.facts = ["存在 prepared transaction 不等于事务异常，可能是正常两阶段提交"]
            twopc_item.causes = ["正常 2PC 进行中", "异常残留（节点故障后未清理）"]
            twopc_item.fix_hint = "结合 prepared 时间与 pg_clean 机制判断是否需要清理（不自动清理）"
    except Exception as exc:
        twopc_item.status = Status.ERROR
        twopc_item.summary = str(exc)[:200]
    report.items.append(twopc_item)

    # 数据分布与倾斜（只读：xc_node_id 分组行数；大表默认不精确扫描）
    report.items.append(_skew_item(conn, nodes, cfg))

    _append_log_checks(report, cfg, nodes)
    if conn:
        try:
            conn.close()
        except Exception:
            pass
    return report


def _processes_item() -> Item:
    item = Item(id="node.processes", name="节点进程", status=Status.PASS)
    rc, out, err = _run(["ps", "-eo", "pid,user,args", "--width", "200"], timeout=5)
    if rc != 0:
        item.status = Status.ERROR
        item.summary = f"ps 执行失败: {err or out}"
        return item
    procs = [l for l in out.splitlines() if "postgres" in l or "gtm" in l]
    coord_n = sum(1 for l in procs if "--coordinator" in l)
    dn_n = sum(1 for l in procs if "--datanode" in l)
    gtm_n = sum(1 for l in procs if re.search(r"/bin/gtm\b", l))
    item.evidence = [f"coordinator 进程 {coord_n} 个 / datanode 进程 {dn_n} 个 / gtm 进程 {gtm_n} 个"]
    item.summary = f"CN={coord_n} DN={dn_n} GTM={gtm_n}"
    item.source = ["ps -eo pid,user,args"]
    if coord_n < 1:
        item.status = Status.FAIL
        item.summary += "（未见 coordinator 进程）"
    return item


def _skew_stats_hint(conn, item: Item, qualified: str, oid, nsp: str, rel: str, pcattnum_text: str) -> None:
    """零扫描倾斜风险提示（pg_stats）。仅作提示，不参与状态判定：统计信息≠实测倾斜。"""
    attnums: list[int] = []
    for tok in (pcattnum_text or "").split():
        try:
            attnums.append(int(tok))
        except ValueError:
            return
    if not attnums:
        return
    try:
        rows, _ = run_sql(conn, f"SELECT attname FROM pg_attribute WHERE attrelid={int(oid)} "
                                f"AND attnum IN ({','.join(str(a) for a in attnums)}) ORDER BY attnum")
    except Exception:
        return
    colnames = [r[0] for r in rows]
    if not colnames:
        return
    try:
        srows, _ = run_sql(conn, "SELECT attname, n_distinct::text, most_common_freqs::text FROM pg_stats "
                                 f"WHERE schemaname={_qstr(nsp)} AND tablename={_qstr(rel)}")
    except Exception:
        return
    stats = {r[0]: r for r in srows}
    risks = []
    for col in colnames:
        s = stats.get(col)
        if not s:
            continue
        nd_text, freqs_text = s[1], s[2]
        nd = None
        if nd_text:
            try:
                nd = float(nd_text)
            except ValueError:
                pass
        f0 = None
        if freqs_text:
            m = re.match(r"\{(-?[\d.eE+]+)", freqs_text)
            if m:
                try:
                    f0 = float(m.group(1))
                except ValueError:
                    pass
        msgs = []
        if nd is not None and nd < 0 and nd != -1 and abs(nd) < 0.1:
            # PG 语义：负值=不同值数/行数（-1 特指与行数相等，无倾斜风险）
            msgs.append(f"n_distinct 为比例型（{nd_text}，不同值占比约 {abs(nd):.0%}）")
        elif nd is not None and 0 < nd < 4:
            msgs.append(f"不同值仅 {nd:g} 个")
        if f0 is not None and f0 >= 0.5:
            msgs.append(f"最常见值占 {f0:.0%}")
        if msgs:
            risks.append(f"{col}（{'；'.join(msgs)}）")
    if risks:
        item.evidence.append(
            f"{qualified}: 零扫描倾斜风险提示（pg_stats）分布列 " + "、".join(risks)
            + "；未做精确统计，可用 --skew-tables 验证"
        )


def _skew_item(conn, nodes: list[dict], cfg: InspectorConfig) -> Item:
    item = Item(id="data.skew", name="数据分布与倾斜检查", status=Status.PASS)
    item.source = ["pg_class", "pgxc_class", "pg_stats", "xc_node_id 分组行数"]
    dn_count = len([n for n in nodes if n["node_type"] == "D"])
    if dn_count <= 1:
        item.status = Status.SKIP
        item.summary = f"数据节点 {dn_count} 个，不存在跨节点倾斜概念"
        return item
    id_to_name = {n.get("node_id"): n["node_name"] for n in nodes if n.get("node_id") is not None}
    try:
        rows, _ = run_sql(conn,
            "SELECT c.oid, n.nspname, c.relname, c.reltuples::bigint, "
            "x.pclocatortype::text, x.discolnums::text "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pgxc_class x ON x.pcrelid = c.oid "
            "WHERE c.relkind IN ('r','p') AND n.nspname NOT IN "
            "('pg_catalog','information_schema','pg_toast') "
            "ORDER BY n.nspname, c.relname;")
    except Exception as exc:
        item.status = Status.ERROR
        item.summary = f"分布元数据读取失败: {str(exc)[:200]}"
        return item
    if not rows:
        item.status = Status.SKIP
        item.summary = "未发现用户表"
        return item

    scanned = 0
    skipped = 0
    warn_tables: list[str] = []
    fail_tables: list[str] = []
    scan_errors: list[str] = []
    for oid, nsp, rel, reltuples, locator, pcattnum in rows:
        qualified = f"{nsp}.{rel}"
        if not locator:
            item.evidence.append(f"{qualified}: 无 pgxc_class 分布信息（本地表/未分布式），跳过")
            skipped += 1
            continue
        if locator == "R":
            item.evidence.append(f"{qualified}: 复制分布（各 DN 全量副本），无倾斜概念")
            skipped += 1
            continue
        explicit = qualified in cfg.skew_tables or rel in cfg.skew_tables
        if not explicit:
            if reltuples is None or reltuples < 0:
                item.evidence.append(
                    f"{qualified}: 未收集统计信息（reltuples={reltuples}），默认不精确扫描；可用 --skew-tables 显式指定")
                _skew_stats_hint(conn, item, qualified, oid, nsp, rel, pcattnum)
                skipped += 1
                continue
            if reltuples > cfg.skew_max_rows:
                item.evidence.append(
                    f"{qualified}: reltuples={reltuples} 超过 --skew-max-rows={cfg.skew_max_rows}，"
                    "默认不精确扫描（安全设计）；可用 --skew-tables 显式指定")
                _skew_stats_hint(conn, item, qualified, oid, nsp, rel, pcattnum)
                skipped += 1
                continue
        try:
            rows2, _ = run_sql(conn, f"SELECT xc_node_id, count(*) FROM {_qident(nsp)}.{_qident(rel)} "
                                     "GROUP BY xc_node_id ORDER BY xc_node_id")
        except Exception as exc:
            scan_errors.append(f"{qualified}: {str(exc)[:120]}")
            item.evidence.append(f"{qualified}: 行数统计失败 {str(exc)[:120]}")
            continue
        counts = {rid: cnt for rid, cnt in rows2}
        total = sum(counts.values())
        if total == 0:
            item.evidence.append(f"{qualified}: 空表（0 行）")
            scanned += 1
            continue
        parts = [f"{id_to_name.get(rid, f'node_{rid}')}={cnt}" for rid, cnt in sorted(counts.items())]
        missing = dn_count - len(counts)
        if missing > 0:
            parts.append(f"其余 {missing} 个 DN=0 行")
        maxv = max(counts.values())
        minv = min(counts.values()) if missing == 0 else 0
        ratio = maxv / minv if minv else float("inf")
        line = f"{qualified}: " + " ".join(parts) + f"（总计 {total}，最大/最小={ratio:.2f}）"
        if minv == 0:
            if total >= cfg.skew_fail_min_rows:
                fail_tables.append(qualified)
                line += " → 完全倾斜（存在 0 行 DN）"
            else:
                warn_tables.append(qualified)
                line += " → 完全倾斜但总行数低于 FAIL 阈值，降级 WARN"
        elif ratio >= SKEW_WARN_RATIO:
            warn_tables.append(qualified)
            line += " → 行数不均衡"
        else:
            line += " → 均衡"
        item.evidence.append(line)
        scanned += 1

    if scan_errors:
        item.status = Status.ERROR
        item.summary = f"{len(scan_errors)} 张表行数统计失败（证据不完整）"
        item.causes = ["查询超时/权限不足/表被并发删除；失败表不参与判定"]
    elif fail_tables:
        item.status = Status.FAIL
        item.summary = f"{len(fail_tables)} 张表完全倾斜：{', '.join(fail_tables)}"
        item.causes = ["分布列取值高度集中（哈希分片后数据落在单一 DN）", "分布列选择不当或数据本身特征", "也可能是有意为之，需结合业务意图判断"]
        item.fix_hint = "核对分布列（pgxc_class.pcattnum）；调整分布需重建表：CREATE TABLE ... DISTRIBUTE BY SHARD(列) 后迁移数据"
    elif warn_tables:
        item.status = Status.WARN
        item.summary = f"{len(warn_tables)} 张表行数不均衡：{', '.join(warn_tables)}"
        item.causes = ["分布列不同值数量少或取值偏斜", "不均衡不等于故障：不影响正确性，主要影响并行查询性能与节点负载"]
        item.fix_hint = "结合业务评估分布列选择；调整需重建表（见 error-cases.md 数据倾斜条目）"
    elif scanned:
        item.summary = f"{scanned} 张分片表行数均衡" + (f"（{skipped} 张复制/本地/超阈值表跳过）" if skipped else "")
    else:
        item.summary = f"未精确统计任何分片表（{skipped} 张复制分布/无分布信息/超阈值）"
    if skipped:
        item.facts.append(f"{skipped} 张表未精确扫描（复制分布/无分布信息/规模阈值），不参与倾斜判定")
    return item


def _append_node_level_checks(report: Report, cfg: InspectorConfig) -> None:
    """数据库不可用时仍执行的检查：备选端口、进程、日志（日志在 _append_log_checks）。"""
    port_item = Item(id="pgxc.reg_vs_ports", name="注册信息与实际监听一致性", status=Status.SKIP)
    if not cfg.ports:
        port_item.summary = "数据库不可用且未提供 --ports，跳过端口一致性检查"
    else:
        port_item.status = Status.PASS
        try:
            listening = _ss_listening_ports()
            port_item.source = ["ss -ltn"]
            for p in cfg.ports:
                if p in listening:
                    port_item.evidence.append(f"端口 {p}: 监听中")
                else:
                    port_item.status = Status.FAIL
                    port_item.evidence.append(f"端口 {p}: 未监听")
            port_item.summary = ("存在未监听端口" if port_item.status == Status.FAIL
                                 else "提供的端口均处于监听状态")
            if port_item.status == Status.FAIL:
                port_item.causes = ["对应节点进程未运行"]
                port_item.fix_hint = "用 pg_ctl/gtm_ctl status 核对；检查节点日志"
        except OSError as exc:
            port_item.status = Status.ERROR
            port_item.summary = str(exc)
    report.items.append(port_item)
    report.items.append(_processes_item())


def _append_log_checks(report: Report, cfg: InspectorConfig, nodes: list[dict]) -> None:
    """日志扫描与跨节点关联。文件不可读只影响本项，不中断巡检。"""
    roles = _roles_from_log_files(cfg)
    roles.update(cfg.node_roles)
    roles.update({n["node_name"]: _node_role(n["node_name"], n["node_type"]) for n in nodes})
    window = timedelta(hours=cfg.log_window_hours)
    cutoff = log_parser.now_utc() - window

    scan_item = Item(id="logs.scan", name="错误日志扫描", status=Status.PASS)
    entries: dict[str, list[log_parser.LogEntry]] = {}
    read_errors: list[str] = []
    unparsed_total = 0
    for node, fname in cfg.log_files.items():
        path = os.path.join(cfg.log_dir, fname) if not os.path.isabs(fname) else fname
        role = roles.get(node, "unknown")
        try:
            es, unparsed = log_parser.parse_file(path, node=node)
        except OSError as exc:
            read_errors.append(f"{path}: {exc}")
            continue
        unparsed_total += unparsed
        entries[node] = es
        errors = [e for e in es if e.level in log_parser.ERROR_LEVELS and e.ts >= cutoff]
        if errors:
            scan_item.evidence.append(f"{node}: 窗口内 ERROR/FATAL {len(errors)} 条")
            for e in errors[:3]:
                scan_item.evidence.append(f"  [{e.ts:%m-%d %H:%M:%S}] {e.level}: {e.message[:90]}")
        else:
            scan_item.evidence.append(f"{node}: 窗口内无 ERROR/FATAL")
    if read_errors:
        scan_item.status = Status.ERROR
        scan_item.summary = f"{len(read_errors)} 个日志不可读（证据不完整）"
        scan_item.evidence += read_errors
        scan_item.causes = ["日志文件权限不足（实验环境为 opentenbase:600），需以 root/属主运行"]
    else:
        total_err = sum(
            1 for es in entries.values() for e in es if e.level in log_parser.ERROR_LEVELS and e.ts >= cutoff
        )
        scan_item.summary = f"窗口内 {total_err} 条 ERROR/FATAL"
        if total_err:
            scan_item.status = Status.WARN
            scan_item.facts = ["历史/窗口内错误不等于当前故障，需结合发生时间与关联判断"]
    scan_item.source = [os.path.join(cfg.log_dir, f) if not os.path.isabs(f) else f for f in cfg.log_files.values()]
    scan_item.facts.append(f"未解析行共 {unparsed_total}（格式不符的非标准行）")
    report.items.append(scan_item)

    # 跨节点关联：CN 与各 DN
    cn_nodes = [n for n, r in roles.items() if r == "coordinator"]
    dn_nodes = [n for n, r in roles.items() if r == "datanode"]
    corr_item = Item(id="logs.correlate", name="跨节点日志关联", status=Status.PASS)
    if not (cn_nodes and dn_nodes) or not entries:
        corr_item.status = Status.SKIP
        corr_item.summary = "缺少 CN 或 DN 日志，跳过关联"
        report.items.append(corr_item)
        return
    cn_entries = [e for n in cn_nodes for e in entries.get(n, []) if e.ts >= cutoff]
    dn_entries = [e for n in dn_nodes for e in entries.get(n, []) if e.ts >= cutoff]
    corr = log_parser.correlate(cn_entries, dn_entries)
    corr_in_window = [c for c in corr if log_parser.corr_in_window(c, cutoff)]
    for c in corr_in_window:
        report.correlations.append(Correlation(
            coord=c["coord"],
            cn_entry=c["cn_entry"],
            dn_entries=c["dn_entries"],
            confidence=c["confidence"],
            reason=c["reason"],
        ))
    confirmed = [c for c in corr_in_window if c["confidence"] == "confirmed"]
    corr_item.evidence = [
        f"confirmed {len(confirmed)} / candidate {len([c for c in corr_in_window if c['confidence']=='candidate'])} / insufficient {len([c for c in corr_in_window if c['confidence']=='insufficient'])}"
    ]
    corr_item.facts.append("关联置信度只说明两侧日志是否属同一执行上下文（关联可靠性），不等于故障根因已确认")
    if confirmed:
        corr_item.status = Status.FAIL
        corr_item.summary = f"{len(confirmed)} 条确认关联（CN/DN 双侧事件，属明确异常）"
        corr_item.causes = ["窗口内存在跨节点故障事件，详见 correlations 输出"]
        corr_item.fix_hint = "结合确认关联的 coord 与错误文本签名（diagnose）进一步定位根因，勿把关联确认当作根因结论"
    else:
        corr_item.summary = "窗口内无确认关联事件"
    report.items.append(corr_item)
