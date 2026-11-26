"""inspect 子命令：运行期集群巡检（只读）。

检查范围：CN 连接、pgxc_node/pgxc_group 注册、注册端口与实际监听/可达性、
节点进程、CN→DN 直连、跨节点生效配置对比、会话与连接水位、残留 prepared
事务、数据分布倾斜、日志错误扫描与 coord 跨节点关联。全部数据库查询走只读
会话；节点连接失败只影响该节点相关检查，其余检查继续。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import timedelta

from . import log_parser, net
from .db import connect, redact_conninfo, run_sql
from .models import Correlation, Item, Report, Status

# 不含用户名/密码的默认连接串：凭据必须由调用方通过 --conn 显式提供
DEFAULT_CONNINFO = "host=127.0.0.1 port=15001"
DEFAULT_LOG_DIR = "/data/opentenbase-lab/logs"
DEFAULT_LOG_FILES = {"cn": "cn.log", "dn1": "dn1.log", "dn2": "dn2.log"}
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
    except OSError as exc:
        return 126, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"command timed out after {timeout}s"


def _db_error_item(name: str, exc: Exception) -> Item:
    item = Item(id=name, name=name, status=Status.ERROR)
    item.summary = str(exc)[:200]
    return item


def _query_nodes(conn) -> tuple[list[dict], str]:
    rows, cols = run_sql(
        conn,
        "SELECT oid, node_name, node_type, node_port, node_host, nodeis_primary, node_id "
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
        conn_item.fix_hint = "用 --conn 提供完整连接串并核对 CN 端口监听；端口/进程/日志检查已独立执行"
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

    # 注册信息 vs 实际监听/可达性
    port_item = Item(id="pgxc.reg_vs_ports", name="注册信息与实际监听一致性", status=Status.PASS)
    try:
        listening = net.ss_listening_ports()
        port_item.source = ["pgxc_node", "ss -ltn"]
        local_checked = False
        for n in nodes:
            try:
                port = int(n["node_port"])
            except (TypeError, ValueError):
                port_item.status = Status.FAIL
                port_item.evidence.append(f"{n['node_name']}: 注册端口无效（{n.get('node_port')!r}），无法核对")
                continue
            host = (n.get("node_host") or "").strip()
            if not host or net.is_local_host(host):
                local_checked = True
                if port in listening:
                    port_item.evidence.append(f"{n['node_name']}: 本机端口 {port} 监听中")
                else:
                    port_item.status = Status.FAIL
                    port_item.evidence.append(f"{n['node_name']}: 本机端口 {port} 未监听")
            else:
                if net.tcp_port_open(host, port):
                    port_item.evidence.append(f"{n['node_name']}: {host}:{port} TCP 可达")
                else:
                    port_item.status = Status.FAIL
                    port_item.evidence.append(f"{n['node_name']}: {host}:{port} TCP 不可达")
        if local_checked:
            port_item.facts.append("本机监听表示端口被本机进程占用，进程归属由 node.processes 与 conn.direct_dn 核对")
        if port_item.status == Status.FAIL:
            port_item.summary = "存在注册端口未监听或不可达的节点（节点可能宕机）"
            port_item.causes = ["节点进程未运行", "节点崩溃或未随集群启动", "远程节点网络不可达"]
            port_item.fix_hint = "用 pg_ctl/gtm_ctl status 核对；检查 /data 下节点日志"
        else:
            port_item.summary = "注册端口均处于监听/可达状态"
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
        try:
            port = int(n["node_port"])
        except (TypeError, ValueError):
            direct_item.status = Status.FAIL
            direct_item.evidence.append(f"{label}: 注册端口无效（{n.get('node_port')!r}），无法构造直连")
            continue
        dn_conn = None
        try:
            dn_conn = connect(
                _conninfo_for_node(cfg.conninfo, host=n.get("node_host") or "", port=port),
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
    perr: dict[str, list[str]] = {}
    targets = []
    skipped_gtm = []
    for n in nodes:
        if n["node_type"] not in ("C", "D"):
            if n["node_type"] == "G":
                skipped_gtm.append(n["node_name"])
            continue
        raw_port = n.get("node_port")
        if raw_port in (None, ""):
            effective[n["node_name"]] = {"_error": "注册信息缺少端口，无法构造连接"}
            continue
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            effective[n["node_name"]] = {"_error": f"注册端口无效（{raw_port!r}），无法构造连接"}
            continue
        # C/D 统一按注册 host/port 构造连接：多 CN 部署下每个 CN 连自己的注册地址
        targets.append((
            n["node_name"],
            _conninfo_for_node(cfg.conninfo, host=n.get("node_host") or "", port=port),
        ))
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
                    perr.setdefault(p, []).append(name)
            c.close()
            effective[name] = vals
        except Exception as exc:
            effective[name] = {"_error": str(exc)[:120]}
    for name, vals in effective.items():
        if "_error" in vals:
            cfg_item.evidence.append(f"{name}: 采集失败 {vals['_error']}")
        else:
            cfg_item.evidence.append(f"{name}: " + " ".join(f"{k}={v}" for k, v in vals.items() if v is not None))
    collected = {n: v for n, v in effective.items() if "_error" not in v}
    diffs: list[tuple[str, list]] = []
    for param in COMPARABLE_PARAMS:
        values = {v.get(param) for v in collected.values() if isinstance(v.get(param), str)}
        if len(values) > 1:
            diffs.append((param, sorted(values)))
    errors = [name for name, vals in effective.items() if "_error" in vals]
    if skipped_gtm:
        cfg_item.facts.append(f"{'、'.join(skipped_gtm)} 为 GTM 协议节点，不参与配置对比")
    # 参数级采集失败分级：全部可达节点均失败（total_fail，证据缺失）与部分节点
    # 失败（partial_fail，对比基于已采集子集）状态语义不同，不得混为「取值一致」。
    total_fail = [
        p for p in COMPARABLE_PARAMS
        if perr.get(p) and not any(isinstance(v.get(p), str) for v in collected.values())
    ]
    partial_fail = [
        p for p in COMPARABLE_PARAMS
        if perr.get(p) and any(isinstance(v.get(p), str) for v in collected.values())
    ]
    if diffs:
        cfg_item.facts.append(f"参数 {', '.join(p for p, _ in diffs)} 在已采集节点间取值不同"
                              + (f"（仅 {len(collected)}/{len(effective)} 个节点采集成功，统计基于该子集）" if errors else ""))
    if partial_fail:
        cfg_item.facts.append(f"参数 {', '.join(partial_fail)} 在部分节点采集失败（对比基于已采集数据）")
    if not targets and not effective:
        cfg_item.status = Status.SKIP
        cfg_item.summary = "注册节点中无 C/D 节点可对比配置"
    elif errors:
        cfg_item.status = Status.ERROR
        if len(errors) == len(effective) and effective:
            cfg_item.summary = "所有节点配置采集失败"
        else:
            cfg_item.summary = "部分节点配置采集失败（其余检查继续）"
        cfg_item.causes = ["连接失败/权限不足/查询超时；配置差异统计基于采集成功的节点"]
    elif total_fail:
        cfg_item.status = Status.ERROR
        cfg_item.summary = f"参数 {', '.join(total_fail)} 在所有可达节点均采集失败（证据不完整）"
        cfg_item.causes = ["权限不足", "参数在当前版本不存在（OpenTenBase 与 PG 的 GUC 差异）", "查询超时"]
    elif diffs:
        cfg_item.status = Status.WARN
        cfg_item.summary = "节点间存在参数差异"
        cfg_item.causes = ["参数差异可能来自部署意图（如各节点规格不同），结合部署配置判断是否需要统一"]
    elif partial_fail:
        cfg_item.status = Status.WARN
        cfg_item.summary = f"参数 {', '.join(partial_fail)} 在部分节点采集失败（对比基于已采集数据）"
        cfg_item.causes = ["权限不足或版本差异导致部分节点无法读取该参数；一致结论不覆盖未采集的节点"]
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
        cn_names = [n["node_name"] for n in nodes if n["node_type"] == "C"]
        cn_label = cn_names[0] if len(cn_names) == 1 else "(CN)"
        counts: dict[str, int] = {}
        null_nodename = False
        rows, _ = run_sql(
            conn,
            "SELECT nodename, count(*) FROM pg_stat_cluster_activity "
            "WHERE backend_type = 'client backend' GROUP BY nodename;",
        )
        for r in rows:
            if r[0]:
                counts[r[0]] = r[1]
            else:
                counts[cn_label] = counts.get(cn_label, 0) + r[1]
                null_nodename = True
        evaluated = 0
        for name, maxv in effective.items():
            if not isinstance(maxv.get("max_connections"), str):
                continue
            cnt = counts.get(name)
            if cnt is None:
                continue
            evaluated += 1
            ratio = cnt / int(maxv["max_connections"])
            wm_item.evidence.append(f"{name}: {cnt}/{maxv['max_connections']} ({ratio:.0%})")
            if ratio >= cfg.watermark_threshold:
                wm_item.status = Status.WARN
        if wm_item.status == Status.WARN:
            wm_item.summary = f"水位达到阈值 {cfg.watermark_threshold:.0%}"
            wm_item.causes = ["连接数接近上限，新连接可能被拒绝（53300）"]
            wm_item.fix_hint = "排查空闲连接与连接泄漏；如确认需要可评估调大 max_connections（需重启）"
        elif evaluated == 0:
            wm_item.status = Status.SKIP
            wm_item.summary = "无 max_connections 数据可用，未评估连接水位"
            wm_item.facts = ["水位依赖 cfg.effective 采集的 max_connections 与 pg_stat_cluster_activity 计数，数据缺失时不推断"]
            if null_nodename:
                wm_item.facts.append("部分会话记录 nodename 为空；多 CN 环境下无法归因到具体节点，未参与水位计算")
        else:
            wm_item.summary = f"水位正常（评估 {evaluated} 个节点）"
            if null_nodename:
                wm_item.facts.append("部分会话记录 nodename 为空；多 CN 环境下无法归因到具体节点，未参与水位计算")
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
            twopc_item.facts = ["prepared transaction 是两阶段提交的正常中间状态；异常残留常见于节点故障后未清理"]
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


def _skew_stats_hint(conn, item: Item, qualified: str, oid, nsp: str, rel: str, discolnums_text: str) -> None:
    """零扫描倾斜风险提示（pg_stats）。仅作提示，不参与状态判定：统计信息是估算值。"""
    attnums: list[int] = []
    for tok in (discolnums_text or "").split():
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
    dn_names = [n["node_name"] for n in nodes if n["node_type"] == "D"]
    if len(dn_names) <= 1:
        item.status = Status.SKIP
        item.summary = f"数据节点 {len(dn_names)} 个，不存在跨节点倾斜概念"
        return item
    id_to_name = {n.get("node_id"): n["node_name"] for n in nodes if n.get("node_id") is not None}
    oid_to_name = {n.get("oid"): n["node_name"] for n in nodes if n.get("oid") is not None}
    try:
        rows, _ = run_sql(conn,
            "SELECT c.oid, n.nspname, c.relname, c.reltuples::bigint, "
            "x.pclocatortype::text, x.discolnums::text, x.nodeoids::text "
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
    meta_tables: list[str] = []
    fail_tables: list[str] = []
    scan_errors: list[str] = []
    for oid, nsp, rel, reltuples, locator, discolnums_text, nodeoids_text in rows:
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
                _skew_stats_hint(conn, item, qualified, oid, nsp, rel, discolnums_text)
                skipped += 1
                continue
            if reltuples > cfg.skew_max_rows:
                item.evidence.append(
                    f"{qualified}: reltuples={reltuples} 超过 --skew-max-rows={cfg.skew_max_rows}，"
                    "默认不精确扫描；可用 --skew-tables 显式指定")
                _skew_stats_hint(conn, item, qualified, oid, nsp, rel, discolnums_text)
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
        unknown_ids = sorted(rid for rid in counts if rid not in id_to_name)
        # 期望节点范围：pgxc_class.nodeoids（表级分布节点）优先；缺失/无法映射时按集群 DN 集。
        # 节点范围不可靠（meta_problem）时单独报告，不基于不可靠范围下均衡结论。
        expected_names: list[str] | None = None
        meta_problem = False
        if nodeoids_text and oid_to_name:
            oids = []
            unparsed = 0
            for tok in nodeoids_text.split():
                try:
                    oids.append(int(tok))
                except ValueError:
                    unparsed += 1
            matched = [oid_to_name[o] for o in oids if o in oid_to_name]
            if oids and len(matched) == len(oids):
                expected_names = matched
                if unparsed:
                    meta_problem = True
                    item.facts.append(
                        f"{qualified}: pgxc_class.nodeoids 含 {unparsed} 个无法解析的节点标识（节点范围可能不完整）")
            elif oids:
                meta_problem = True
                item.facts.append(
                    f"{qualified}: pgxc_class.nodeoids 中 {len(oids) - len(matched)} 个节点 oid 无法映射到 pgxc_node（节点范围不可靠）")
            elif unparsed:
                meta_problem = True
                item.facts.append(f"{qualified}: pgxc_class.nodeoids 无法解析（节点范围不可靠）")
        expected = set(expected_names) if expected_names is not None else set(dn_names)
        actual_names = {id_to_name[rid] for rid in counts if rid in id_to_name}
        unexpected = sorted(actual_names - expected)
        if unexpected:
            meta_problem = True
            item.facts.append(f"{qualified}: 行数统计含分布范围外的节点 {unexpected}（分布元数据与注册信息不一致）")
        if unknown_ids:
            meta_problem = True
            item.facts.append(
                f"{qualified}: 行数统计含未注册到 pgxc_node 的 xc_node_id {unknown_ids}（分布元数据与注册信息不一致）")
        # 节点范围可靠（nodeoids 完整映射或回退集群 DN 集，且无范围外/未注册节点）
        # 才可断言 0 行节点；范围不可靠时只报元数据异常，不下倾斜结论。
        missing = sorted(expected - actual_names) if not meta_problem else []
        if missing:
            parts.append(f"{'、'.join(missing)}=0 行")
        maxv = max(counts.values())
        minv = min(counts.values()) if not missing else 0
        ratio = maxv / minv if minv else float("inf")
        line = f"{qualified}: " + " ".join(parts) + f"（总计 {total}，最大/最小={ratio:.2f}）"
        if missing:
            if total >= cfg.skew_fail_min_rows:
                fail_tables.append(qualified)
                line += " → 完全倾斜（存在 0 行节点）"
            else:
                warn_tables.append(qualified)
                line += " → 完全倾斜但总行数低于 FAIL 阈值，降级 WARN"
        elif meta_problem:
            meta_tables.append(qualified)
            line += " → 分布元数据异常（节点范围不可靠），不判定均衡"
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
        item.causes = ["分布列取值高度集中（哈希分片后数据落在单一节点）", "分布列选择不当或数据本身特征", "也可能是有意为之，需结合业务意图判断"]
        item.fix_hint = "核对分布列（pgxc_class.discolnums）；调整分布需重建表：CREATE TABLE ... DISTRIBUTE BY SHARD(列) 后迁移数据"
    elif warn_tables or meta_tables:
        item.status = Status.WARN
        parts_s = []
        if warn_tables:
            parts_s.append(f"{len(warn_tables)} 张表行数不均衡：{', '.join(warn_tables)}")
        if meta_tables:
            parts_s.append(f"{len(meta_tables)} 张表分布元数据异常：{', '.join(meta_tables)}")
        item.summary = "；".join(parts_s)
        item.causes = ["分布列不同值数量少或取值偏斜",
                       "行数不均衡影响并行查询性能与节点负载均衡，不影响查询正确性；结合业务数据特征评估分布列",
                       "分布元数据与注册信息不一致（nodeoids 无法映射、出现范围外节点）时，节点范围不可靠，不判定均衡"]
        item.fix_hint = "结合业务评估分布列选择；调整需重建表（见 error-cases.md 数据倾斜条目）；元数据异常先核对 pgxc_class 与 pgxc_node 的对应关系"
    elif scanned:
        item.summary = f"{scanned} 张分片表行数均衡" + (f"（{skipped} 张复制/本地/超阈值表跳过）" if skipped else "")
    else:
        item.status = Status.SKIP
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
            listening = net.ss_listening_ports()
            port_item.source = ["ss -ltn"]
            for p in cfg.ports:
                if p in listening:
                    port_item.evidence.append(f"端口 {p}: 监听中")
                else:
                    port_item.status = Status.FAIL
                    port_item.evidence.append(f"端口 {p}: 未监听")
            port_item.summary = ("存在未监听端口" if port_item.status == Status.FAIL
                                 else "提供的端口均处于监听状态")
            port_item.facts = ["数据库不可用时按 --ports 做本机监听检查；远程节点可达性需数据库恢复后按注册信息核对"]
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
    failed_nodes: set[str] = set()
    unparsed_total = 0
    format_mismatch: list[str] = []
    for node, fname in cfg.log_files.items():
        path = os.path.join(cfg.log_dir, fname) if not os.path.isabs(fname) else fname
        role = roles.get(node, "unknown")
        try:
            es, unparsed = log_parser.parse_file(path, node=node)
        except OSError as exc:
            read_errors.append(f"{path}: {exc}")
            failed_nodes.add(node)
            continue
        unparsed_total += unparsed
        entries[node] = es
        if not es and unparsed > 0:
            format_mismatch.append(f"{node}: {unparsed} 行全部无法解析")
            failed_nodes.add(node)
        errors = [e for e in es if e.level in log_parser.ERROR_LEVELS and e.ts >= cutoff]
        if errors:
            scan_item.evidence.append(f"{node}: 窗口内 ERROR/FATAL {len(errors)} 条")
            for e in errors[:3]:
                scan_item.evidence.append(f"  [{e.ts:%m-%d %H:%M:%S}] {e.level}: {e.message[:90]}")
        else:
            scan_item.evidence.append(f"{node}: 窗口内无 ERROR/FATAL" if es else f"{node}: 空日志文件")
    if read_errors:
        scan_item.status = Status.ERROR
        scan_item.summary = f"{len(read_errors)} 个日志不可读（证据不完整）"
        scan_item.evidence += read_errors
        scan_item.causes = ["日志文件权限不足（实验环境为 opentenbase:600），需以 root/属主运行"]
    elif format_mismatch:
        scan_item.status = Status.ERROR
        scan_item.summary = f"{len(format_mismatch)} 个日志文件全部行无法解析（格式与解析器不匹配，错误扫描与关联无证据）"
        scan_item.evidence += format_mismatch
        scan_item.causes = [
            "日志格式与默认 log_line_prefix 不一致（需 '2026-11-20 17:43:54.411 CST [pid,coord(p,v)] LEVEL: message' 形式）",
            "文件非 stderr 文本日志（如 csvlog/二进制）",
        ]
    else:
        total_err = sum(
            1 for es in entries.values() for e in es if e.level in log_parser.ERROR_LEVELS and e.ts >= cutoff
        )
        scan_item.summary = f"窗口内 {total_err} 条 ERROR/FATAL"
        if total_err:
            scan_item.status = Status.WARN
            scan_item.facts = ["窗口内错误已按时间列出；结合发生时间与关联判断与当前问题的相关性"]
    scan_item.source = [os.path.join(cfg.log_dir, f) if not os.path.isabs(f) else f for f in cfg.log_files.values()]
    if unparsed_total:
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
    corr_item.facts.append("关联置信度表示两侧日志是否属于同一执行上下文；错误签名用于提供可能原因及排查建议（见 diagnose）")
    if confirmed:
        corr_item.status = Status.FAIL
        corr_item.summary = f"{len(confirmed)} 条确认关联（CN/DN 双侧事件，属明确异常）"
        corr_item.causes = ["窗口内存在跨节点故障事件，详见 correlations 输出"]
        corr_item.fix_hint = "结合确认关联的 coord 与错误文本签名（diagnose）定位具体原因"
    elif set(cn_nodes) & failed_nodes or set(dn_nodes) & failed_nodes:
        corr_item.status = Status.ERROR
        corr_item.summary = "部分 CN/DN 日志缺失或无法解析，关联证据不完整"
        corr_item.causes = ["无确认关联不代表无跨节点事件：缺失侧日志可能正包含对应记录"]
    else:
        corr_item.summary = "窗口内无确认关联事件"
    report.items.append(corr_item)
