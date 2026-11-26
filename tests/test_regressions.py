"""回归测试。

覆盖：
  1. 关联查询时间窗口过滤（窗口过滤在关联前；两侧事件不同时在窗口内不得进入结果；
     不因此过度限制正常关联——配对窗口 30s 语义保留）；
  2. 连接串重建（带引号/空格与百分号编码密码不截断不误码）与密码脱敏
     （超时消息不回显命令、probe_env 密钥脱敏、redact_conninfo 八类格式）；
  3. DN 直连与生效配置使用 pgxc_node 注册 host（Mock 多主机集群）；
  4. cfg.effective 采集失败（ERROR）不被参数差异（WARN）覆盖；
  5. db.connect 强制 autocommit（单条语句失败不毒化后续查询 25P02）与
     直连连接在查询异常时被关闭；
  6. SQLSTATE 57P01 / XX000 规则不单一归因。
全部使用虚构凭据，不含任何真实连接信息。
"""

import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from otb_tools import diagnostics, inspector, net, preflight, rules
from otb_tools.models import Report, Status

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _log_line(ts: datetime, pid: int, coord_pid: int, coord_vxid: int,
              level: str, message: str) -> str:
    return (f"{ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} UTC "
            f"[{pid},coord({coord_pid},{coord_vxid})] {level}:  {message}\n")


# ---------------------------------------------------------------- 问题 1

class TestCorrelationWindowPrefilter:
    """查询时间窗口过滤必须先于关联分组；窗口外事件不得出现在关联结果中。"""

    def _write(self, tmp_path, cn_ts, cn_level, dn_lines):
        cn = tmp_path / "cn.log"
        dn = tmp_path / "dn.log"
        cn.write_text(_log_line(cn_ts, 1000, 500, 1, cn_level, "cn side event"))
        dn.write_text("".join(dn_lines))
        return str(cn), str(dn)

    def test_dn_out_of_window_excluded(self, tmp_path):
        now = datetime.now(timezone.utc)
        cn_path, dn_path = self._write(
            tmp_path,
            now - timedelta(hours=1), "ERROR",
            [_log_line(now - timedelta(hours=30), 2000, 500, 1, "FATAL", "OLD OUT-OF-WINDOW DN EVENT")],
        )
        result = diagnostics.diagnose_logs([cn_path], [dn_path], timedelta(hours=24))
        corr = result["correlations"]
        assert len(corr) == 1
        c = corr[0]
        assert c["confidence"] == "insufficient"  # CN 侧在窗口内、DN 侧被过滤 → 证据不完整
        assert "窗口外" in c["reason"]
        # 窗口外事件不得出现在任何关联结果的事件列表中
        for cor in corr:
            for e in cor["dn_entries"]:
                assert datetime.fromisoformat(e["time"]) >= now - timedelta(hours=24)

    def test_cn_out_of_window_dn_in_window(self, tmp_path):
        now = datetime.now(timezone.utc)
        cn_path, dn_path = self._write(
            tmp_path,
            now - timedelta(hours=30), "ERROR",
            [_log_line(now - timedelta(hours=1), 2000, 500, 1, "FATAL", "IN-WINDOW DN EVENT")],
        )
        result = diagnostics.diagnose_logs([cn_path], [dn_path], timedelta(hours=24))
        corr = result["correlations"]
        assert len(corr) == 1
        assert corr[0]["confidence"] == "insufficient"
        assert corr[0]["cn_entry"] == {}  # CN 侧窗口外事件被过滤，不得冒充同上下文证据
        assert "窗口外" in corr[0]["reason"]

    def test_mixed_same_coord_only_in_window_events_grouped(self, tmp_path):
        """同一 coord 键混入窗口外事件时，只有窗口内事件进入关联结果（原缺陷反例）。"""
        now = datetime.now(timezone.utc)
        in_ts = now - timedelta(hours=1)
        cn_path, dn_path = self._write(
            tmp_path, in_ts, "ERROR",
            [
                _log_line(in_ts, 2000, 500, 1, "FATAL", "CURRENT DN FATAL"),
                _log_line(now - timedelta(hours=30), 2001, 500, 1, "FATAL", "OLD DN FATAL"),
            ],
        )
        result = diagnostics.diagnose_logs([cn_path], [dn_path], timedelta(hours=24))
        corr = result["correlations"]
        assert len(corr) == 1
        c = corr[0]
        assert c["confidence"] == "confirmed"
        assert len(c["dn_entries"]) == 1
        assert "CURRENT" in c["dn_entries"][0]["message"]
        assert all(
            datetime.fromisoformat(e["time"]) >= now - timedelta(hours=24)
            for e in c["dn_entries"]
        )

    def test_pairing_window_still_allows_normal_correlation(self, tmp_path):
        """两侧事件均在查询窗口内、间隔 < 配对窗口 30s → 仍 confirmed（不因修复而过度限制）。"""
        now = datetime.now(timezone.utc)
        cn_ts = now - timedelta(minutes=5)
        dn_ts = cn_ts + timedelta(seconds=20)
        cn = tmp_path / "cn.log"
        dn = tmp_path / "dn.log"
        cn.write_text(_log_line(cn_ts, 1000, 700, 2, "ERROR", "cn event"))
        dn.write_text(_log_line(dn_ts, 2000, 700, 2, "FATAL", "dn event"))
        result = diagnostics.diagnose_logs([str(cn)], [str(dn)], timedelta(hours=24))
        assert result["correlations"][0]["confidence"] == "confirmed"

    def test_diagnose_multiple_cn_files_with_colliding_key_is_candidate(self, tmp_path):
        """diagnose 只有 coordinator 角色时以来源文件区分潜在 CN，歧义降级。"""
        now = datetime.now(timezone.utc)
        t0 = now - timedelta(seconds=60)
        cn1, cn2, dn = tmp_path / "cn-a.log", tmp_path / "cn-b.log", tmp_path / "dn.log"
        cn1.write_text(_log_line(t0, 1000, 500, 1, "ERROR", "first CN"))
        cn2.write_text(_log_line(t0 + timedelta(seconds=40), 1001, 500, 1, "LOG", "second CN"))
        dn.write_text(_log_line(t0 + timedelta(seconds=20), 2000, 500, 1, "WARNING", "DN event"))

        result = diagnostics.diagnose_logs(
            [str(cn1), str(cn2)], [str(dn)], timedelta(hours=24),
        )
        corr = next(c for c in result["correlations"] if c["coord"] == "500,1")
        assert corr["confidence"] == "candidate"
        assert "多个不同/不明 CN 来源" in corr["reason"]

    def test_boundary_events_included_excluded(self, tmp_path):
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        cn = tmp_path / "cn.log"
        dn = tmp_path / "dn.log"
        cn.write_text(_log_line(cutoff - timedelta(minutes=5), 1, 1, 1, "ERROR", "OUT"))
        dn.write_text(_log_line(cutoff + timedelta(minutes=5), 2, 1, 1, "FATAL", "IN"))
        result = diagnostics.diagnose_logs([str(cn)], [str(dn)], timedelta(hours=24))
        assert result["error_fatal_in_window"] == 1  # 恰在边界内的事件计入
        corr = result["correlations"]
        assert len(corr) == 1
        assert corr[0]["confidence"] == "insufficient"  # CN 侧为边界外事件

    def test_inspect_log_checks_prefilter(self, tmp_path):
        now = datetime.now(timezone.utc)
        in_ts = now - timedelta(hours=1)
        cn = tmp_path / "cn.log"
        dn = tmp_path / "dn1.log"
        cn.write_text(_log_line(in_ts, 1000, 500, 1, "ERROR", "cn event"))
        dn.write_text(
            _log_line(in_ts, 2000, 500, 1, "FATAL", "CURRENT DN FATAL")
            + _log_line(now - timedelta(hours=30), 2001, 500, 1, "FATAL", "OLD DN FATAL")
        )
        cfg = inspector.InspectorConfig(
            log_dir=str(tmp_path),
            log_files={"cn": "cn.log", "dn1": "dn1.log"},
            node_roles={"cn": "coordinator", "dn1": "datanode"},
            log_window_hours=24,
        )
        report = Report(command="inspect")
        inspector._append_log_checks(report, cfg, [])
        confirmed = [c for c in report.correlations if c.confidence == "confirmed"]
        assert confirmed
        assert len(confirmed[0].dn_entries) == 1
        assert "CURRENT" in confirmed[0].dn_entries[0]["message"]


# ---------------------------------------------------------------- 问题 2

class TestConninfoRebuildAndRedaction:
    def test_conninfo_for_port_quoted_password_with_spaces(self):
        from psycopg2.extensions import parse_dsn

        out = inspector._conninfo_for_port(
            "host=127.0.0.1 port=15001 dbname=lab user=student password='secret with spaces'",
            15401,
        )
        dsn = parse_dsn(out)  # 旧实现产出未闭合引号 → 截断 → 此处解析失败或值错误
        assert dsn["password"] == "secret with spaces"
        assert dsn["port"] == "15401"
        assert dsn["dbname"] == "lab"
        assert dsn["user"] == "student"

    def test_conninfo_for_port_uri_percent_encoded_password(self):
        from psycopg2.extensions import parse_dsn

        out = inspector._conninfo_for_port(
            "postgresql://stu:p%40ss@127.0.0.1:15001/lab", 15402
        )
        dsn = parse_dsn(out)
        assert dsn["password"] == "p@ss"  # 旧实现不解码 → 连接时认证失败
        assert dsn["port"] == "15402"
        assert dsn["user"] == "stu"
        assert dsn["dbname"] == "lab"

    def test_conninfo_for_node_query_param_password_kept(self):
        from psycopg2.extensions import parse_dsn

        out = inspector._conninfo_for_node(
            "postgresql://user@host/db?password=secret&sslmode=require",
            host="192.0.2.11", port=15401,
        )
        dsn = parse_dsn(out)
        assert dsn["password"] == "secret"
        assert dsn["host"] == "192.0.2.11"
        assert dsn["port"] == "15401"

    def test_conninfo_for_node_empty_host_keeps_original(self):
        from psycopg2.extensions import parse_dsn

        out = inspector._conninfo_for_node("host=10.0.0.1 user=u", host="", port=15401)
        dsn = parse_dsn(out)
        assert dsn["host"] == "10.0.0.1"  # 注册信息缺 host 时降级沿用原 host
        assert dsn["port"] == "15401"

    def test_preflight_timeout_does_not_echo_command(self):
        rc, out, err = preflight._run(["sleep", "3"], timeout=0.2)
        assert rc == 124
        assert err == "command timed out after 0.2s"
        assert "sleep" not in err  # 超时消息不得回显命令行（可能含连接串/密码）

    def test_db_probe_timeout_evidence_redacted(self, monkeypatch):
        monkeypatch.setattr(
            preflight, "_run",
            lambda cmd, env=None, timeout=10: (124, "", "command timed out after 10s"),
        )
        cfg = preflight.PreflightConfig(
            conninfo="host=127.0.0.1 port=15001 dbname=lab user=student password=sup3rs3cret",
            probe_env={"PGPASSWORD": "envpass9", "LD_LIBRARY_PATH": "/opt/x"},
        )
        item = preflight._check_db_probe(cfg)
        blob = json.dumps(item.to_dict(), ensure_ascii=False)
        assert "sup3rs3cret" not in blob
        assert "envpass9" not in blob
        assert "***" in blob  # 脱敏占位符存在
        assert "password=***" in item.source[0]

    @pytest.mark.parametrize("conninfo,secrets,marker", [
        ("host=x port=5432 dbname=d user=u password=secret", ["secret"], "***"),
        ("host=x port=5432 dbname=d user=u password = secret", ["secret"], "***"),
        ("host=x password='secret with spaces'", ["secret"], "***"),
        ('host=x password="secret with spaces"', ["secret"], "***"),
        ("postgresql://user:secret@host:5432/db", ["secret"], "***"),
        ("postgresql://user:secret@host/db?sslmode=require", ["secret"], "***"),
        # URI 查询参数形式的占位符经 urlencode 后为 %2A%2A%2A
        ("postgresql://user@host/db?password=secret", ["secret"], "%2A%2A%2A"),
        ("postgresql://user:p%40ss@host:5432/db", ["p%40ss", "p@ss"], "***"),
    ])
    def test_redact_conninfo_cases(self, conninfo, secrets, marker):
        from otb_tools.db import redact_conninfo

        out = redact_conninfo(conninfo)
        for s in secrets:
            assert s not in out
        assert marker in out


# ---------------------------------------------------------------- 问题 3

NODE_COLS = ["oid", "node_name", "node_type", "node_port", "node_host", "nodeis_primary", "node_id"]


def _mock_cluster(monkeypatch, tmp_path, nodes, values, fail_ports=(),
                  run_sql_raise_ports=(), raise_params=(), tcp_open=True,
                  param_fail_ports=None):
    """构造 Mock 多主机集群：CN 192.0.2.10:15001，DN 注册 host 与 CN 不同。

    返回 (captured_conninfos, conns)。fake_run_sql 按查询文本分派；values
    为 {port: {param: value}}；fail_ports 中的端口 connect 抛 ConnectionError；
    run_sql_raise_ports 中的端口在 current_setting('port') 时抛 RuntimeError；
    raise_params 中的参数在所有节点的 current_setting 查询抛 RuntimeError；
    param_fail_ports（{port: [param, ...]}）限定参数在部分节点失败。
    """
    from psycopg2.extensions import parse_dsn

    param_fail_ports = param_fail_ports or {}

    class FakeConn:
        def __init__(self, conninfo):
            self.conninfo = conninfo
            self.closed = False

        def close(self):
            self.closed = True

    captured = []
    conns = []

    def fake_connect(conninfo, timeout=5, **kw):
        captured.append(conninfo)
        port = int(parse_dsn(conninfo)["port"])
        if port in fail_ports:
            raise ConnectionError("could not connect to server: Connection refused")
        c = FakeConn(conninfo)
        conns.append(c)
        return c

    def fake_run_sql(conn, sql, params=None):
        port = int(parse_dsn(conn.conninfo)["port"])
        if port in run_sql_raise_ports and "current_setting('port')" in sql:
            raise RuntimeError("simulated query failure")
        if "current_setting" in sql:
            p = sql.split("'")[1]
            if p in raise_params or p in param_fail_ports.get(port, []):
                raise RuntimeError(f"simulated param query failure: {p}")
            if p == "port":
                return [(str(port),)], ["current_setting"]
            return [(values[port][p],)], ["current_setting"]
        if "FROM pgxc_node" in sql:
            return [tuple(n) for n in nodes], NODE_COLS
        if "FROM pgxc_group" in sql:
            return [("default_group", 0, 0)], ["group_name", "group_id", "group_members"]
        if "pg_stat_cluster_activity" in sql:
            return [], ["nodename", "state", "count"]
        if "pg_prepared_xacts" in sql:
            return [], ["gid", "prepared", "owner"]
        if "FROM pg_class" in sql and "pgxc_class" in sql:
            # data.skew 分布元数据：Mock 集群无用户表 → SKIP
            return [], ["oid", "nspname", "relname", "reltuples", "pclocatortype", "discolnums", "nodeoids"]
        raise AssertionError(f"unexpected sql: {sql}")

    monkeypatch.setattr(inspector, "connect", fake_connect)
    monkeypatch.setattr(inspector, "run_sql", fake_run_sql)
    monkeypatch.setattr(net, "ss_listening_ports", lambda: {15001, 15401, 15402, 5000})
    monkeypatch.setattr(net, "tcp_port_open", lambda host, port, timeout=3.0: tcp_open)
    monkeypatch.setattr(
        inspector, "_run",
        lambda cmd, timeout=5: (
            0, "123 opentenbase postgres -D /data/cn --coordinator -p 15001", ""
        ),
    )
    return captured, conns


class TestDnRegisteredHost:
    NODES = [
        (100, "cn0001", "C", 15001, "192.0.2.10", True, 0),
        (101, "dn0001", "D", 15401, "192.0.2.11", True, 1),
        (102, "dn0002", "D", 15402, "192.0.2.12", True, 2),
    ]

    def test_direct_dn_and_effective_use_registered_host(self, monkeypatch, tmp_path):
        from psycopg2.extensions import parse_dsn

        values = {p: {
            "max_connections": "100", "shared_buffers": "128MB",
            "log_destination": "stderr", "logging_collector": "on",
            "max_prepared_transactions": "10",
        } for p in (15001, 15401, 15402)}
        captured, conns = _mock_cluster(monkeypatch, tmp_path, self.NODES, values)
        cfg = inspector.InspectorConfig(
            conninfo="host=192.0.2.10 port=15001 dbname=lab user=student password=stu",
            log_dir=str(tmp_path), log_files={},
        )
        report = inspector.run(cfg)
        items = {i.id: i for i in report.items}

        # 连接顺序：CN（巡检入口）、dn1、dn2（直连）、cn0001、dn1、dn2（生效配置，含 CN 自身）
        assert len(captured) == 6
        cn_dsn = parse_dsn(captured[0])
        assert cn_dsn["host"] == "192.0.2.10" and cn_dsn["port"] == "15001"
        for idx, (host, port) in enumerate([("192.0.2.11", "15401"),
                                            ("192.0.2.12", "15402")], start=1):
            dsn = parse_dsn(captured[idx])
            assert dsn["host"] == host and dsn["port"] == port
            assert dsn["user"] == "student" and dsn["dbname"] == "lab"
            assert "192.0.2.10" not in captured[idx]  # 不得沿用 CN 的 host
        # 生效配置采集同样按注册地址：CN 连自身注册地址，DN 连各自注册 host
        for idx, (host, port) in enumerate([("192.0.2.10", "15001"),
                                            ("192.0.2.11", "15401"),
                                            ("192.0.2.12", "15402")], start=3):
            dsn = parse_dsn(captured[idx])
            assert dsn["host"] == host and dsn["port"] == port

        direct = items["conn.direct_dn"]
        assert direct.status == Status.PASS
        assert any("192.0.2.11:15401" in e for e in direct.evidence)
        assert any("192.0.2.12:15402" in e for e in direct.evidence)
        assert items["cfg.effective"].status == Status.PASS
        assert all(c.closed for c in conns)  # 所有连接（含直连 DN）均已关闭
        assert report.overall_exit_code() == 0

    def test_unreachable_dn_reported_not_guessed(self, monkeypatch, tmp_path):
        values = {p: {"max_connections": "100"} for p in (15001, 15401, 15402)}
        _mock_cluster(monkeypatch, tmp_path, self.NODES, values, fail_ports=(15401,))
        cfg = inspector.InspectorConfig(
            conninfo="host=192.0.2.10 port=15001 user=student",
            log_dir=str(tmp_path), log_files={},
        )
        report = inspector.run(cfg)
        items = {i.id: i for i in report.items}
        direct = items["conn.direct_dn"]
        assert direct.status == Status.FAIL
        assert any("192.0.2.11:15401" in e and "直连失败" in e for e in direct.evidence)
        # 直连失败按实际结果报告；部分失败不影响可达 DN 的报告
        assert any("直连成功" in e for e in direct.evidence)

    def test_missing_port_node_reported_not_crash(self, monkeypatch, tmp_path):
        nodes = self.NODES + [(103, "dn0003", "D", None, "192.0.2.13", True, 3)]
        values = {p: {"max_connections": "100"} for p in (15001, 15401, 15402)}
        _mock_cluster(monkeypatch, tmp_path, nodes, values)
        cfg = inspector.InspectorConfig(
            conninfo="host=192.0.2.10 port=15001 user=student",
            log_dir=str(tmp_path), log_files={},
        )
        report = inspector.run(cfg)  # 旧实现 int(None) 直接崩溃
        items = {i.id: i for i in report.items}
        assert items["pgxc.reg_vs_ports"].status == Status.FAIL
        assert any("注册端口无效" in e for e in items["pgxc.reg_vs_ports"].evidence)
        direct = items["conn.direct_dn"]
        assert any("注册信息缺少端口" in e for e in direct.evidence)
        cfg_item = items["cfg.effective"]
        assert cfg_item.status == Status.ERROR
        assert any("缺少端口" in e for e in cfg_item.evidence)

    def test_gtm_node_skipped_in_effective_cfg(self, monkeypatch, tmp_path):
        from psycopg2.extensions import parse_dsn

        nodes = self.NODES + [(104, "gtm0001", "G", 5000, "192.0.2.50", True, 4)]
        values = {p: {
            "max_connections": "100", "shared_buffers": "128MB",
            "log_destination": "stderr", "logging_collector": "on",
            "max_prepared_transactions": "10",
        } for p in (15001, 15401, 15402)}
        captured, _ = _mock_cluster(monkeypatch, tmp_path, nodes, values)
        cfg = inspector.InspectorConfig(
            conninfo="host=192.0.2.10 port=15001 user=student",
            log_dir=str(tmp_path), log_files={},
        )
        report = inspector.run(cfg)
        cfg_item = next(i for i in report.items if i.id == "cfg.effective")
        # GTM 为 GTM 协议节点，不得用 PG 协议连接采集配置（实测连接即被关闭）
        assert all(parse_dsn(c)["port"] != "5000" for c in captured)
        assert cfg_item.status == Status.PASS
        assert any("GTM 协议节点" in f for f in cfg_item.facts)

    def test_invalid_registered_port_reported_not_crash(self, monkeypatch, tmp_path):
        # 非法端口字符串（非数字）：cfg.effective 构造连接串前须捕获，不得崩溃
        nodes = self.NODES + [(103, "dn0003", "D", "abc", "192.0.2.13", True, 3)]
        values = {p: {"max_connections": "100"} for p in (15001, 15401, 15402)}
        _mock_cluster(monkeypatch, tmp_path, nodes, values)
        cfg = inspector.InspectorConfig(
            conninfo="host=192.0.2.10 port=15001 user=student",
            log_dir=str(tmp_path), log_files={},
        )
        report = inspector.run(cfg)  # 旧实现 cfg.effective 在 try/except 外 int() 直接崩溃
        items = {i.id: i for i in report.items}
        cfg_item = items["cfg.effective"]
        assert cfg_item.status == Status.ERROR
        assert any("注册端口无效" in e for e in cfg_item.evidence)
        assert any("注册端口无效" in e for e in items["conn.direct_dn"].evidence)

    def test_default_log_files_exclude_gtm(self):
        cfg = inspector.InspectorConfig(conninfo="host=x port=1 user=u")
        # GTM 日志非 PG stderr 格式（实测全部行无法解析），默认只扫 CN/DN
        assert "gtm" not in cfg.log_files
        assert set(cfg.log_files) == {"cn", "dn1", "dn2"}


# ---------------------------------------------------------------- 问题 4

class TestMultiCnAndParamFailures:
    MULTI_CN = [
        (100, "cn0001", "C", 15001, "192.0.2.10", True, 0),
        (101, "cn0002", "C", 15002, "192.0.2.20", True, 1),
        (102, "dn0001", "D", 15401, "192.0.2.11", True, 2),
        (103, "dn0002", "D", 15402, "192.0.2.12", True, 3),
    ]

    @staticmethod
    def _values(ports):
        return {p: {
            "max_connections": "100",
            "shared_buffers": "128MB",
            "log_destination": "stderr",
            "logging_collector": "on",
            "max_prepared_transactions": "10",
        } for p in ports}

    def test_multi_cn_effective_connects_each_registered_address(self, monkeypatch, tmp_path):
        from psycopg2.extensions import parse_dsn

        values = self._values((15001, 15002, 15401, 15402))
        captured, _ = _mock_cluster(monkeypatch, tmp_path, self.MULTI_CN, values)
        cfg = inspector.InspectorConfig(
            conninfo="host=192.0.2.10 port=15001 dbname=lab user=student",
            log_dir=str(tmp_path), log_files={},
        )
        report = inspector.run(cfg)
        cn2 = [c for c in captured if parse_dsn(c)["port"] == "15002"]
        assert cn2 and all(parse_dsn(c)["host"] == "192.0.2.20" for c in cn2)
        assert any("cn0002" in e for e in next(i for i in report.items if i.id == "cfg.effective").evidence)
        assert report.overall_exit_code() == 0

    def test_param_level_failure_all_nodes_is_error_exit_2(self, monkeypatch, tmp_path):
        values = self._values((15001, 15401, 15402))
        _mock_cluster(monkeypatch, tmp_path, TestDnRegisteredHost.NODES, values,
                      raise_params=("max_connections",))
        cfg = inspector.InspectorConfig(
            conninfo="host=192.0.2.10 port=15001 user=student",
            log_dir=str(tmp_path), log_files={},
        )
        report = inspector.run(cfg)
        cfg_item = next(i for i in report.items if i.id == "cfg.effective")
        assert cfg_item.status == Status.ERROR  # 所有可达节点该参数均采集失败 → 证据不完整
        assert "证据不完整" in cfg_item.summary
        assert report.overall_exit_code() == 2

    def test_watermark_without_data_skips(self, monkeypatch, tmp_path):
        values = self._values((15001, 15401, 15402))
        _mock_cluster(monkeypatch, tmp_path, TestDnRegisteredHost.NODES, values)
        cfg = inspector.InspectorConfig(
            conninfo="host=192.0.2.10 port=15001 user=student",
            log_dir=str(tmp_path), log_files={},
        )
        report = inspector.run(cfg)
        wm = next(i for i in report.items if i.id == "sessions.watermark")
        assert wm.status == Status.SKIP  # 无会话计数数据时不推断水位
        assert "未评估连接水位" in wm.summary

    def test_tcp_unreachable_registered_port_fails(self, monkeypatch, tmp_path):
        values = self._values((15001, 15401, 15402))
        _mock_cluster(monkeypatch, tmp_path, TestDnRegisteredHost.NODES, values,
                      tcp_open=False)
        cfg = inspector.InspectorConfig(
            conninfo="host=192.0.2.10 port=15001 user=student",
            log_dir=str(tmp_path), log_files={},
        )
        report = inspector.run(cfg)
        port_item = next(i for i in report.items if i.id == "pgxc.reg_vs_ports")
        assert port_item.status == Status.FAIL
        assert any("TCP 不可达" in e for e in port_item.evidence)
        assert report.overall_exit_code() == 1

class TestEffectiveConfigStatusPrecedence:
    @staticmethod
    def _values(ports):
        return {p: {
            "max_connections": "100",
            "shared_buffers": "128MB",
            "log_destination": "stderr",
            "logging_collector": "on",
            "max_prepared_transactions": "10",
        } for p in ports}

    def _run(self, monkeypatch, tmp_path, values, fail_ports=()):
        nodes = TestDnRegisteredHost.NODES
        _mock_cluster(monkeypatch, tmp_path, nodes, values, fail_ports=fail_ports)
        cfg = inspector.InspectorConfig(
            conninfo="host=192.0.2.10 port=15001 user=student",
            log_dir=str(tmp_path), log_files={},
        )
        return inspector.run(cfg)

    def test_collect_failure_kept_over_warn_diff(self, monkeypatch, tmp_path):
        # CN ok(100) / DN1 连接失败 / DN2 ok(80)：存在差异但采集不完整 → ERROR 优先
        values = self._values((15001, 15401, 15402))
        values[15402]["max_connections"] = "80"
        report = self._run(monkeypatch, tmp_path, values, fail_ports=(15401,))
        cfg_item = next(i for i in report.items if i.id == "cfg.effective")
        assert cfg_item.status == Status.ERROR  # 旧实现被后续 WARN 覆盖
        assert "部分节点配置采集失败" in cfg_item.summary
        assert any("仅 2/3" in f for f in cfg_item.facts)  # 差异结论注明采集不完整
        assert report.overall_exit_code() == 2  # ERROR 优先级最高

    def test_diff_all_collected_is_warn_exit_0(self, monkeypatch, tmp_path):
        values = self._values((15001, 15401, 15402))
        values[15401]["max_connections"] = "80"
        values[15402]["max_connections"] = "80"
        report = self._run(monkeypatch, tmp_path, values)
        cfg_item = next(i for i in report.items if i.id == "cfg.effective")
        assert cfg_item.status == Status.WARN
        assert "节点间存在参数差异" in cfg_item.summary
        assert any("max_connections" in f for f in cfg_item.facts)
        assert report.overall_exit_code() == 0  # 纯差异（全部采集成功）不触发非零

    def test_query_failure_on_one_dn_direct_item_fail(self, monkeypatch, tmp_path):
        # 单节点查询异常：直连项按失败报告 FAIL，配置项按已采集值继续，连接被关闭
        values = self._values((15001, 15401, 15402))
        captured, conns = _mock_cluster(monkeypatch, tmp_path, TestDnRegisteredHost.NODES,
                                        values, run_sql_raise_ports=(15401,))
        cfg = inspector.InspectorConfig(
            conninfo="host=192.0.2.10 port=15001 user=student",
            log_dir=str(tmp_path), log_files={},
        )
        report = inspector.run(cfg)
        items = {i.id: i for i in report.items}
        assert items["conn.direct_dn"].status == Status.FAIL
        assert any("simulated query failure" in e for e in items["conn.direct_dn"].evidence)
        dn1_conns = [c for c in conns if "15401" in c.conninfo]
        assert dn1_conns and all(c.closed for c in dn1_conns)  # 异常路径连接不泄漏

    def test_partial_param_failure_no_diff_is_warn_exit_0(self, monkeypatch, tmp_path):
        # max_connections 仅 dn1 采集失败：不得输出无条件「取值一致」PASS
        values = self._values((15001, 15401, 15402))
        _mock_cluster(monkeypatch, tmp_path, TestDnRegisteredHost.NODES, values,
                      param_fail_ports={15401: ["max_connections"]})
        cfg = inspector.InspectorConfig(
            conninfo="host=192.0.2.10 port=15001 user=student",
            log_dir=str(tmp_path), log_files={},
        )
        report = inspector.run(cfg)
        cfg_item = next(i for i in report.items if i.id == "cfg.effective")
        assert cfg_item.status == Status.WARN
        assert "部分节点采集失败" in cfg_item.summary
        assert any("max_connections" in f and "部分节点采集失败" in f for f in cfg_item.facts)
        assert report.overall_exit_code() == 0

    def test_partial_param_failure_with_diff_still_warn(self, monkeypatch, tmp_path):
        # 部分参数失败 + 已采集参数存在差异：WARN 差异结论，facts 注明采集不完整
        values = self._values((15001, 15401, 15402))
        values[15402]["shared_buffers"] = "64MB"
        _mock_cluster(monkeypatch, tmp_path, TestDnRegisteredHost.NODES, values,
                      param_fail_ports={15401: ["max_connections"]})
        cfg = inspector.InspectorConfig(
            conninfo="host=192.0.2.10 port=15001 user=student",
            log_dir=str(tmp_path), log_files={},
        )
        report = inspector.run(cfg)
        cfg_item = next(i for i in report.items if i.id == "cfg.effective")
        assert cfg_item.status == Status.WARN
        assert "节点间存在参数差异" in cfg_item.summary
        assert any("部分节点采集失败" in f for f in cfg_item.facts)
        assert report.overall_exit_code() == 0


# ---------------------------------------------------------------- 问题 5

class TestTransactionIsolation:
    def test_connect_sets_autocommit(self, monkeypatch):
        fake_mod = types.ModuleType("psycopg2")

        class FakeConn:
            def __init__(self):
                self.autocommit = False

        fake_mod.connect = lambda conninfo, **kw: FakeConn()
        monkeypatch.setitem(sys.modules, "psycopg2", fake_mod)

        from otb_tools import db

        conn = db.connect("host=x user=u", timeout=3)
        assert conn.autocommit is True  # 白盒不变式：单条语句失败不毒化后续查询（25P02）

    def test_run_sql_preserves_original_error_text(self, monkeypatch):
        fake_mod = types.ModuleType("psycopg2")

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params):
                raise Exception("permission denied for relation pg_file_settings")

        class FakeConn:
            def cursor(self):
                return FakeCursor()

        fake_mod.connect = lambda conninfo, **kw: FakeConn()
        monkeypatch.setitem(sys.modules, "psycopg2", fake_mod)

        from otb_tools import db

        conn = db.connect("host=x")
        with pytest.raises(RuntimeError) as ei:
            db.run_sql(conn, "SELECT * FROM pg_file_settings;")
        assert "permission denied" in str(ei.value)  # 原始失败证据保留，不吞异常


# ---------------------------------------------------------------- 问题 6

class TestSqlstateRuleAttribution:
    def test_57p01_not_single_cause(self):
        case = rules.SQLSTATE_TABLE["57P01"]
        assert len(case["causes"]) >= 2
        assert any("Issue #194" in c for c in case["causes"])  # 分布式查询期间 DN 不可用也可表现 57P01

    def test_xx000_mentions_feature_not_supported(self):
        case = rules.SQLSTATE_TABLE["XX000"]
        assert any("0A000" in c for c in case["causes"])  # Issue #507：不支持功能被误报为 XX000
        assert any("不必然" in c for c in case["causes"])

    def test_diagnose_57p01_multicause_conclusion(self):
        d = diagnostics.diagnose_sqlstate("57P01")
        assert d["matched"] is True
        assert len(d["possible_causes"]) >= 2
        assert "可由不同原因触发" in d["conclusion"]
        # 2026-11-23 ECS 实测升级：客户端 psql 捕获 FATAL (57P01)，管理员终止路径已复现
        assert d["confidence"] == "confirmed"

    def test_diagnose_xx000_mentions_0a000(self):
        d = diagnostics.diagnose_sqlstate("XX000")
        assert d["matched"] is True
        assert any("0A000" in c for c in d["possible_causes"])
        # 2026-11-25 ECS 实测复现 Issue #507 → confidence 升级为 confirmed
        assert d["confidence"] == "confirmed"

    def test_xx000_reproduced_and_signature(self):
        # 2026-11-25 ECS 实测：Issue #507 两条触发语句均报 XX000
        case = rules.SQLSTATE_TABLE["XX000"]
        assert case["otb_note"].startswith("reproduced")
        assert any("global index" in c for c in case["causes"])
        hits = rules.match_signatures("ERROR: (XX000) not support global index yet")
        ids = {h["id"] for h in hits}
        assert "xx000-not-supported" in ids
        hit = next(h for h in hits if h["id"] == "xx000-not-supported")
        assert hit["evidence_basis"].startswith("reproduced")
        assert any("0A000" in c for c in hit["possible_causes"])
        hits2 = rules.match_signatures(
            "ERROR: (XX000) TRIGGER is not supported on replication table in distributed mode"
        )
        assert "xx000-not-supported" in {h["id"] for h in hits2}
        d = diagnostics.diagnose_text("ERROR: (XX000) not support global index yet")
        assert d["confidence"] == "confirmed"

    def test_08000_entry_reproduced_and_signature(self):
        # 2026-11-23 ECS 实测：分布式查询期间杀 DN 后端 → 客户端 08000 Failed to send plan
        case = rules.SQLSTATE_TABLE["08000"]
        assert case["otb_note"].startswith("reproduced")
        assert any("57P01" in n for n in case["next"])
        hits = rules.match_signatures("ERROR:  Failed to send plan on dn0001 pid 7754")
        ids = {h["id"] for h in hits}
        assert "failed-to-send-plan" in ids
        hit = next(h for h in hits if h["id"] == "failed-to-send-plan")
        assert hit["evidence_basis"].startswith("reproduced")
        assert any("57P01" in s for s in hit["next_steps"])
        d = diagnostics.diagnose_text("ERROR:  Failed to send plan on dn0001 pid 7754")
        assert d["confidence"] == "confirmed"

    def test_53000_entry_reproduced_and_signature(self):
        # 2026-11-24 ECS 实测：fast 停 DN1 → CN 客户端 53000 Failed to get pooled connections
        case = rules.SQLSTATE_TABLE["53000"]
        assert case["otb_note"].startswith("reproduced")
        hits = rules.match_signatures(
            "ERROR:  (53000) Failed to get pooled connections: connect for dn0001(127.0.0.1:15401) failed, errmsg: could not connect to server: Connection refused"
        )
        ids = {h["id"] for h in hits}
        assert "failed-to-get-pooled-connections" in ids
        # 拆分为独立签名后，dn-connection-failed 不得再吞并该文本
        assert "dn-connection-failed" not in ids
        d = diagnostics.diagnose_text(
            "ERROR:  (53000) Failed to get pooled connections: connect for dn0001(127.0.0.1:15401) failed"
        )
        assert d["confidence"] == "confirmed"
