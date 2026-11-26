import json
import os

import pytest

from otb_tools import cli, inspector, log_parser, net
from otb_tools.models import Item, Report, Status

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class TestInspectorUnit:
    def test_conninfo_for_port_keyvalue(self):
        out = inspector._conninfo_for_port(
            "host=127.0.0.1 port=15001 dbname=lab user=student password=x", 15401
        )
        assert "port=15401" in out
        assert "port=15001" not in out

    def test_conninfo_for_port_uri(self):
        out = inspector._conninfo_for_port("postgresql://stu:pw@127.0.0.1:15001/lab", 15401)
        assert "port=15401" in out
        assert "dbname=lab" in out

    def test_db_down_degrades_gracefully(self, monkeypatch, tmp_path):
        cfg = inspector.InspectorConfig(
            conninfo="host=127.0.0.1 port=15001",
            log_dir=str(tmp_path),
            log_files={},
        )

        def boom(*a, **k):
            raise ConnectionError("could not connect to server: Connection refused")

        monkeypatch.setattr(inspector, "connect", boom)
        report = inspector.run(cfg)
        items = {i.id: i for i in report.items}
        assert items["db.connect"].status == Status.ERROR
        assert "Connection refused" in items["db.connect"].summary
        # 端口一致性检查被 SKIP，进程检查仍执行
        assert items["pgxc.reg_vs_ports"].status == Status.SKIP
        assert "node.processes" in items
        assert report.overall_exit_code() == 2  # 执行失败/不完整

    def test_driver_missing_reported(self, monkeypatch, tmp_path):
        cfg = inspector.InspectorConfig(conninfo="x", log_dir=str(tmp_path), log_files={})

        def no_psycopg2(*a, **k):
            raise ImportError("No module named 'psycopg2'")

        monkeypatch.setattr(inspector, "connect", no_psycopg2)
        report = inspector.run(cfg)
        assert report.items[0].id == "db.connect"
        assert "psycopg2" in report.items[0].summary

    def test_log_checks_with_fixtures(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone

        monkeypatch.setattr(
            log_parser, "now_utc", lambda: datetime(2026, 11, 21, tzinfo=timezone.utc)
        )
        cfg = inspector.InspectorConfig(
            log_dir="",
            log_files={
                "cn": os.path.join(FIX, "logs", "cn_sample.log"),
                "dn1": os.path.join(FIX, "logs", "dn1_sample.log"),
                "dn2": os.path.join(FIX, "logs", "dn2_sample.log"),
            },
            node_roles={"cn": "coordinator", "dn1": "datanode", "dn2": "datanode"},
            log_window_hours=24 * 30,
        )
        report = Report(command="inspect")
        inspector._append_log_checks(report, cfg, [])
        items = {i.id: i for i in report.items}
        assert items["logs.scan"].status == Status.WARN
        assert "2533,76" in [c.coord for c in report.correlations]
        confirmed = [c for c in report.correlations if c.confidence == "confirmed"]
        assert confirmed

    def test_log_checks_missing_files_error(self, tmp_path):
        cfg = inspector.InspectorConfig(
            log_dir=str(tmp_path),
            log_files={"cn": "missing_cn.log", "dn1": "missing_dn1.log"},
            node_roles={"cn": "coordinator", "dn1": "datanode"},
        )
        report = Report(command="inspect")
        inspector._append_log_checks(report, cfg, [])
        items = {i.id: i for i in report.items}
        assert items["logs.scan"].status == Status.ERROR
        assert "证据不完整" in items["logs.scan"].summary
        assert items["logs.correlate"].status == Status.SKIP

    def test_log_correlate_one_side_missing_is_error(self, tmp_path):
        # 单侧日志缺失时不得输出「窗口内无确认关联事件」PASS
        cn = tmp_path / "cn.log"
        cn.write_text("2026-11-20 17:43:54.411 CST [1000,coord(1000,1)] LOG:  some message\n")
        cfg = inspector.InspectorConfig(
            log_dir=str(tmp_path),
            log_files={"cn": "cn.log", "dn1": "missing_dn1.log"},
        )
        report = Report(command="inspect")
        inspector._append_log_checks(report, cfg, [])
        items = {i.id: i for i in report.items}
        assert items["logs.scan"].status == Status.ERROR
        assert items["logs.correlate"].status == Status.ERROR
        assert "关联证据不完整" in items["logs.correlate"].summary

    def test_log_correlate_one_side_unparsable_is_error(self, tmp_path):
        cn = tmp_path / "cn.log"
        dn = tmp_path / "dn1.log"
        cn.write_text("2026-11-20 17:43:54.411 CST [1000,coord(1000,1)] LOG:  some message\n")
        dn.write_text("not a log line\nanother garbage\n")
        cfg = inspector.InspectorConfig(
            log_dir=str(tmp_path),
            log_files={"cn": "cn.log", "dn1": "dn1.log"},
        )
        report = Report(command="inspect")
        inspector._append_log_checks(report, cfg, [])
        items = {i.id: i for i in report.items}
        assert items["logs.scan"].status == Status.ERROR
        assert items["logs.correlate"].status == Status.ERROR
        assert "关联证据不完整" in items["logs.correlate"].summary

    def test_log_checks_all_empty_files_ok(self, tmp_path):
        # 日志文件存在但为空：正常状态，不报 ERROR
        (tmp_path / "cn.log").write_text("")
        (tmp_path / "dn1.log").write_text("")
        cfg = inspector.InspectorConfig(
            log_dir=str(tmp_path),
            log_files={"cn": "cn.log", "dn1": "dn1.log"},
        )
        report = Report(command="inspect")
        inspector._append_log_checks(report, cfg, [])
        items = {i.id: i for i in report.items}
        assert items["logs.scan"].status == Status.PASS
        assert "窗口内 0 条" in items["logs.scan"].summary
        assert items["logs.correlate"].status == Status.PASS

    def test_dn_only_insufficient_correlation_no_crash(self, tmp_path):
        # 回归：DN 有 coord 键而 CN 无同键记录时，correlation 的 cn_entry 为空，
        # 窗口过滤（corr_in_window）不得因缺失 cn_entry 抛 KeyError。
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        cn = tmp_path / "cn.log"
        dn = tmp_path / "dn1.log"
        cn.write_text(f"{ts} UTC [1000,coord(1000,1)] LOG:  Changing connection state for node dn0001 pid: 2000, state ERROR ==> FATAL, invoker execRemote.c: 862\n")
        dn.write_text(f"{ts} UTC [2000,coord(2000,1)] FATAL:  terminating connection due to administrator command\n")
        cfg = inspector.InspectorConfig(
            log_dir=str(tmp_path),
            log_files={"cn": "cn.log", "dn1": "dn1.log"},
        )
        report = Report(command="inspect")
        inspector._append_log_checks(report, cfg, [])
        items = {i.id: i for i in report.items}
        assert items["logs.correlate"].status != Status.ERROR
        coords = {c.coord: c for c in report.correlations}
        assert coords["2000,1"].confidence == "insufficient"

    def test_roles_inferred_from_log_files_when_db_down(self):
        cfg = inspector.InspectorConfig(log_files={"cn": "cn.log", "dn1": "dn1.log", "gtm": "gtm.log"})
        roles = inspector._roles_from_log_files(cfg)
        assert roles == {"cn": "coordinator", "dn1": "datanode", "gtm": "gtm"}
        cfg2 = inspector.InspectorConfig(log_files={"cn001": "cn001.log", "dn002": "dn002.log"})
        assert inspector._roles_from_log_files(cfg2) == {"cn001": "coordinator", "dn002": "datanode"}

    def test_direct_dn_failure_is_fail_not_error(self, monkeypatch, tmp_path):
        class FakeConn:
            def close(self):
                pass

        calls = {"n": 0}

        def fake_connect(conninfo, timeout=5):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeConn()
            raise ConnectionError("Connection refused")

        def fake_run_sql(conn, sql, params=None):
            if "FROM pgxc_node" in sql:
                return [
                    (100, "cn0001", "C", 15001, "127.0.0.1", True, 0),
                    (101, "dn0001", "D", 15401, "127.0.0.1", True, 1),
                ], ["oid", "node_name", "node_type", "node_port", "node_host", "nodeis_primary", "node_id"]
            if "FROM pgxc_group" in sql:
                return [("default_group", 0, 0)], ["group_name", "group_id", "group_members"]
            if "current_setting" in sql:
                return [("100",)], ["current_setting"]
            return [], ["nodename", "state", "count"]

        monkeypatch.setattr(inspector, "connect", fake_connect)
        monkeypatch.setattr(inspector, "run_sql", fake_run_sql)
        monkeypatch.setattr(net, "ss_listening_ports", lambda: {15001, 15401})
        monkeypatch.setattr(inspector, "_run", lambda cmd, timeout=5: (0, "", ""))
        cfg = inspector.InspectorConfig(
            conninfo="host=127.0.0.1 port=15001 user=student",
            log_dir=str(tmp_path),
            log_files={},
        )
        report = inspector.run(cfg)
        items = {i.id: i for i in report.items}
        assert items["conn.direct_dn"].status == Status.FAIL
        assert any("直连失败" in e for e in items["conn.direct_dn"].evidence)

    def test_db_down_port_fallback_statuses(self, monkeypatch, tmp_path):
        cfg = inspector.InspectorConfig(log_dir=str(tmp_path), log_files={}, ports=[])
        monkeypatch.setattr(inspector, "_run", lambda cmd, timeout=5: (0, "", ""))
        r1 = Report(command="inspect")
        inspector._append_node_level_checks(r1, cfg)
        p1 = {i.id: i for i in r1.items}["pgxc.reg_vs_ports"]
        assert p1.status == Status.SKIP  # 未提供 --ports

        cfg.ports = [15001, 15999]
        monkeypatch.setattr(net, "ss_listening_ports", lambda: {15001})
        r2 = Report(command="inspect")
        inspector._append_node_level_checks(r2, cfg)
        p2 = {i.id: i for i in r2.items}["pgxc.reg_vs_ports"]
        assert p2.status == Status.FAIL  # 有端口未监听

        cfg.ports = [15001]
        r3 = Report(command="inspect")
        inspector._append_node_level_checks(r3, cfg)
        p3 = {i.id: i for i in r3.items}["pgxc.reg_vs_ports"]
        assert p3.status == Status.PASS  # 全部监听（回归：曾误报 SKIP）


class TestModels:
    def test_exit_code_precedence(self):
        r = Report(command="x", items=[Item(id="a", name="a", status=Status.PASS)])
        assert r.overall_exit_code() == 0
        r.items.append(Item(id="b", name="b", status=Status.WARN))
        assert r.overall_exit_code() == 0  # WARN 默认不触发非零
        assert Report(command="x", items=list(r.items), strict=True).overall_exit_code() == 1
        r.items.append(Item(id="c", name="c", status=Status.FAIL))
        assert r.overall_exit_code() == 1
        r.items.append(Item(id="d", name="d", status=Status.ERROR))
        assert r.overall_exit_code() == 2

    def test_execution_and_health_status(self):
        ok = Report(command="x", items=[Item(id="a", name="a", status=Status.PASS)])
        assert ok.execution_status() == "ok"
        assert ok.health_status() == "healthy"
        warn = Report(command="x", items=[Item(id="a", name="a", status=Status.WARN)])
        assert warn.execution_status() == "ok"
        assert warn.health_status() == "degraded"
        fail = Report(command="x", items=[Item(id="a", name="a", status=Status.FAIL)])
        assert fail.health_status() == "failed"
        err = Report(command="x", items=[Item(id="a", name="a", status=Status.ERROR)])
        assert err.execution_status() == "failed"
        assert err.health_status() == "unknown"

    def test_health_status_all_skip_unknown(self):
        r = Report(command="x", items=[Item(id="a", name="a", status=Status.SKIP)])
        assert r.execution_status() == "ok"
        assert r.health_status() == "unknown"  # 全部 SKIP 无数据，不判健康

    def test_json_structure(self):
        r = Report(command="preflight", items=[Item(id="a", name="a", status=Status.PASS)])
        d = json.loads(r.to_json())
        assert d["exit_code"] == 0
        assert d["execution_status"] == "ok"
        assert d["health_status"] == "healthy"
        assert d["summary"] == {"PASS": 1, "WARN": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0}
        assert d["items"][0]["id"] == "a"

    def test_redact_conninfo_uri_form(self):
        from otb_tools.db import redact_conninfo

        out = redact_conninfo("postgresql://stu:pw@127.0.0.1:15001/lab")
        assert "pw" not in out
        assert "stu" in out
        assert "127.0.0.1:15001" in out
        assert redact_conninfo("postgresql://127.0.0.1/lab") == "postgresql://127.0.0.1/lab"
        assert "secret" not in redact_conninfo("host=127.0.0.1 port=15001 password=secret")


class TestCli:
    def test_usage_error_exit_3(self):
        with pytest.raises(SystemExit) as ei:
            cli.main(["preflight", "--ports", "not-a-port"])
        assert ei.value.code == 3

    def test_preflight_port_out_of_range_exit_3(self):
        with pytest.raises(SystemExit) as ei:
            cli.main(["preflight", "--ports", "70000"])
        assert ei.value.code == 3

    def test_inspect_watermark_threshold_out_of_range_exit_3(self):
        with pytest.raises(SystemExit) as ei:
            cli.main(["inspect", "--watermark-threshold", "1.5"])
        assert ei.value.code == 3

    def test_float_params_reject_nan_inf_exit_3(self):
        for args in (
            ["inspect", "--watermark-threshold", "nan"],
            ["inspect", "--watermark-threshold", "inf"],
            ["inspect", "--log-window-hours", "inf"],
            ["preflight", "--disk-min-free-gb", "nan"],
            ["preflight", "--disk-min-free-gb", "inf"],
            ["diagnose", "--window-hours", "inf"],
        ):
            with pytest.raises(SystemExit) as ei:
                cli.main(args)
            assert ei.value.code == 3

    def test_diagnose_window_hours_non_positive_exit_3(self):
        with pytest.raises(SystemExit) as ei:
            cli.main(["diagnose", "--cn-logs", "x.log", "--window-hours", "0"])
        assert ei.value.code == 3

    def test_diagnose_text_and_logs_mixed_exit_3(self):
        assert cli.main(["diagnose", "--text", "Connection refused", "--cn-logs", "x.log"]) == 3

    def test_unknown_command_exit_3(self):
        with pytest.raises(SystemExit) as ei:
            cli.main(["no-such-command"])
        assert ei.value.code == 3

    def test_diagnose_sqlstate_exit(self, capsys):
        rc = cli.main(["diagnose", "--sqlstate", "42704", "--format", "json"])
        out = json.loads(capsys.readouterr().out)
        assert out["matched"] is True
        assert rc == 0  # 分析完成即 0；SQLSTATE 只标识错误类别，不表示当前故障
        capsys.readouterr()
        assert cli.main(["diagnose", "--sqlstate", "42704", "--strict"]) == 1

    def test_diagnose_unknown_sqlstate_exit_0(self, capsys):
        rc = cli.main(["diagnose", "--sqlstate", "0A999"])
        assert rc == 0
        assert "不在当前案例库中" in capsys.readouterr().out

    def test_diagnose_text_file_missing_exit_2(self, capsys):
        rc = cli.main(["diagnose", "--text-file", "/nonexistent/file.txt"])
        assert rc == 2

    def test_diagnose_logs_fixtures_exit_0_and_strict(self, capsys):
        # 历史关联事件成功识别也返回 0（分析完成即 0）；--strict 检测模式返回 1
        args = [
            "diagnose",
            "--cn-logs", os.path.join(FIX, "logs", "cn_sample.log"),
            "--dn-logs", os.path.join(FIX, "logs", "dn1_sample.log") + "," + os.path.join(FIX, "logs", "dn2_sample.log"),
            "--window-hours", "720",
        ]
        rc = cli.main(args)
        out = capsys.readouterr().out
        assert "2533,76" in out
        assert rc == 0
        assert "root_cause" in out  # text 输出含根因未确认说明
        capsys.readouterr()
        assert cli.main(args + ["--strict"]) == 1

    def test_preflight_json_output_valid(self, tmp_path, monkeypatch, capsys):
        from otb_tools import preflight

        os.makedirs(tmp_path / "bin", exist_ok=True)
        for tool in ["postgres", "psql", "gtm_ctl", "pg_ctl"]:
            p = tmp_path / "bin" / tool
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)
        os.makedirs(tmp_path / "data", exist_ok=True)
        os.makedirs(tmp_path / "logs", exist_ok=True)
        monkeypatch.setattr(net, "ss_listening_ports", lambda: set())
        monkeypatch.setattr(preflight, "_run", lambda cmd, env=None, timeout=10: (0, "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6", ""))
        rc = cli.main([
            "preflight",
            "--pg-bin", str(tmp_path / "bin"),
            "--runtime-libpq", str(tmp_path / "runtime"),
            "--data-dirs", str(tmp_path / "data"),
            "--log-dir", str(tmp_path / "logs"),
            "--ports", "5000",
            "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert "exit_code" in out
        assert out["summary"]["PASS"] >= 5

    def test_preflight_strict_warn_exit_1(self, tmp_path, monkeypatch, capsys):
        from otb_tools import preflight

        os.makedirs(tmp_path / "bin", exist_ok=True)
        for tool in ["postgres", "psql", "gtm_ctl", "pg_ctl"]:
            p = tmp_path / "bin" / tool
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)
        os.makedirs(tmp_path / "data", exist_ok=True)
        os.makedirs(tmp_path / "logs", exist_ok=True)
        monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
        monkeypatch.setattr(net, "ss_listening_ports", lambda: set())
        monkeypatch.setattr(preflight, "_run", lambda cmd, env=None, timeout=10: (0, "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6", ""))
        base = [
            "preflight",
            "--pg-bin", str(tmp_path / "bin"),
            "--runtime-libpq", str(tmp_path / "runtime"),
            "--data-dirs", str(tmp_path / "data"),
            "--log-dir", str(tmp_path / "logs"),
            "--ports", "5000",
        ]
        assert cli.main(base) == 0  # WARN 默认不计退出码
        assert cli.main(base + ["--strict"]) == 1

    def test_preflight_no_runtime_db_items(self, tmp_path, monkeypatch):
        from otb_tools import preflight

        cfg = preflight.PreflightConfig(
            pg_bin=str(tmp_path),
            runtime_libpq=str(tmp_path / "runtime"),
            compat_dirs=[],
            data_dirs=[],
            log_dir=str(tmp_path),
            ports=[],
            conninfo="host=127.0.0.1 port=15001",
        )
        monkeypatch.setattr(net, "ss_listening_ports", lambda: set())
        monkeypatch.setattr(preflight, "_run", lambda cmd, env=None, timeout=10: (0, "1", ""))
        report = preflight.run(cfg)
        ids = [i.id for i in report.items]
        assert "db.probe" in ids
        # preflight 只做部署前检查 + 可选部署后连接验证，不再追加 2PC/节点组/水位等运行期检查
        assert not any(i.startswith("db.probe_") for i in ids)
        assert len(ids) == 11

    def test_diagnose_warn_signature_strict(self, capsys):
        text = "FATAL:  terminating connection due to administrator command"
        assert cli.main(["diagnose", "--text", text]) == 0  # WARN 级签名默认不计退出码
        capsys.readouterr()
        assert cli.main(["diagnose", "--text", text, "--strict"]) == 1

    def test_diagnose_text_full_refused_default_0_strict_1(self, capsys):
        text = "psql: could not connect to server: Connection refused"
        assert cli.main(["diagnose", "--text", text]) == 0  # 分析完成；不表示当前故障
        capsys.readouterr()
        assert cli.main(["diagnose", "--text", text, "--strict"]) == 1  # 检测模式：FAIL 签名

    def test_diagnose_text_bare_refused_generic_candidate(self, capsys):
        rc = cli.main(["diagnose", "--text", "Connection refused", "--format", "json"])
        assert rc == 0
        d = json.loads(capsys.readouterr().out)
        assert d["matched"] is True
        assert d["confidence"] == "candidate"
        assert any(m["id"] == "connection-refused-generic" for m in d["matches"])
        # 通用类别签名不得把节点宕机当作确定结论
        causes = "".join(m["possible_causes"][0] for m in d["matches"])
        assert "需结合端口监听状态与实际连接串区分" in causes

    def test_diagnose_bare_log_files_scan_only(self, tmp_path, capsys):
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        path = tmp_path / "scanner.log"  # 路径含 "cn" 子串，但作为裸路径不得被猜为 CN 角色
        path.write_text(f"{ts} UTC [1,coord(1,1)] ERROR:  (22012) division by zero\n")
        rc = cli.main(["diagnose", "--log-files", str(path)])
        out = capsys.readouterr().out
        assert rc == 0  # 窗口内错误单独出现不判故障；无 CN/DN 角色故无确认关联
        assert "correlations: 0" in out
        assert "division by zero" in out

    def test_diagnose_historical_errors_exit_0(self, tmp_path, capsys):
        # 时间窗口外（历史）的 ERROR 不判故障
        old = "2026-11-01 10:00:00.000 UTC [1,coord(1,1)] ERROR:  (22012) division by zero\n"
        cn = tmp_path / "cn.log"
        dn = tmp_path / "dn.log"
        cn.write_text(old)
        dn.write_text(old)
        rc = cli.main([
            "diagnose",
            "--cn-logs", str(cn),
            "--dn-logs", str(dn),
            "--window-hours", "24",
        ])
        out = capsys.readouterr().out
        assert rc == 0
        assert "errors_in_window: 0" in out

    def test_classify_log_files_no_substring_guessing(self):
        from otb_tools.cli import _classify_log_files

        cn, dn, other = _classify_log_files({"cn": "cn.log", "dn1": "dn1.log", "gtm": "gtm.log"})
        assert cn == ["cn.log"]
        assert dn == ["dn1.log"]
        assert other == ["gtm.log"]
        cn2, dn2, other2 = _classify_log_files({"/var/log/scanner.log": "/var/log/scanner.log"})
        assert cn2 == [] and dn2 == [] and other2 == ["/var/log/scanner.log"]

    def test_diagnose_garbage_log_file_exit_2(self, tmp_path, capsys):
        path = tmp_path / "garbage.log"
        path.write_text("not a valid log line\n")
        rc = cli.main(["diagnose", "--log-files", str(path)])
        out = capsys.readouterr().out
        assert rc == 2  # 全部行无法解析：证据不完整
        assert "unparsed_files: 1" in out
        assert "格式与解析器不匹配" in out

    def test_diagnose_output_unwritable_exit_2(self, capsys):
        rc = cli.main(["diagnose", "--sqlstate", "42704", "-o", "/nonexistent-dir/out.txt"])
        assert rc == 2

    def test_preflight_output_unwritable_exit_2(self, tmp_path, monkeypatch):
        from otb_tools import preflight

        os.makedirs(tmp_path / "bin", exist_ok=True)
        for tool in ["postgres", "psql", "gtm_ctl", "pg_ctl"]:
            p = tmp_path / "bin" / tool
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)
        os.makedirs(tmp_path / "data", exist_ok=True)
        os.makedirs(tmp_path / "logs", exist_ok=True)
        monkeypatch.setattr(net, "ss_listening_ports", lambda: set())
        monkeypatch.setattr(preflight, "_run", lambda cmd, env=None, timeout=10: (0, "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6", ""))
        rc = cli.main([
            "preflight",
            "--pg-bin", str(tmp_path / "bin"),
            "--runtime-libpq", str(tmp_path / "runtime"),
            "--data-dirs", str(tmp_path / "data"),
            "--log-dir", str(tmp_path / "logs"),
            "--ports", "5000",
            "-o", os.path.join(str(tmp_path), "no-such-dir", "out.txt"),
        ])
        assert rc == 2  # 检查本身通过，但报告写入失败
