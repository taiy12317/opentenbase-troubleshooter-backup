import os

import pytest

from otb_tools import diagnostics, log_parser, net, preflight
from otb_tools.models import Report, Status
from otb_tools.rules import SQLSTATE_TABLE, match_signatures

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class TestRules:
    @pytest.mark.parametrize(
        "text,expected_id",
        [
            ("psql: error while loading shared libraries: libreadline.so.7: cannot open shared object file", "dynlib-missing"),
            ("Expected OpenTenBase libpq.so.5.10 symlink not found.", "libpq-symlink-mismatch"),
            ("psql: could not connect to server: Connection refused", "connection-refused"),
            ('FATAL:  (28000) role "nobody" does not exist', "role-not-exists"),
            ('FATAL:  (3D000) database "nonexist_db" does not exist', "database-not-exists"),
            ("ERROR:  (42704) PGXC Group nonexist_group: group not defined", "pgxc-group-not-defined"),
            ("ERROR:  default group not defined", "default-group-not-defined"),
            ("FATAL:  the database system is in recovery mode", "recovery-mode"),
            ("FATAL:  terminating connection due to administrator command", "admin-termination"),
            ("WARNING:  because tcp fails to send data, force kill SIGTERM to pid 2545", "tcp-send-fail-kill"),
            ("LOG:  Changing connection state for node dn0001 pid: 2545, state ERROR ==> FATAL, invoker execRemote.c: 862", "conn-state-change"),
            ('ERROR:  (42601) syntax error at or near "SELEC"', "syntax-error"),
            ('ERROR:  (42P01) relation "nonexistent_table" does not exist', "relation-not-exists"),
            ('ERROR:  (42704) type "memories" does not exist', "type-not-exists"),
            ("ERROR:  (22012) division by zero", "division-by-zero"),
            ("ERROR:  (42501) permission denied for relation pg_file_settings", "permission-denied"),
            ("ERROR:  (42883) function pg_catalog.pg_current_setting(unknown) does not exist", "function-not-exists"),
        ],
    )
    def test_signature_hits(self, text, expected_id):
        hits = match_signatures(text)
        assert any(h["id"] == expected_id for h in hits)

    def test_unknown_text_no_hit(self):
        assert match_signatures("something completely unfamiliar") == []

    def test_sqlstate_table_reproduced_vs_community(self):
        assert SQLSTATE_TABLE["42704"]["otb_note"].startswith("reproduced")
        # 2026-11-23 ECS 实测：pg_terminate_backend 终止自身会话，客户端捕获 57P01 → 升级为 reproduced
        assert SQLSTATE_TABLE["57P01"]["otb_note"].startswith("reproduced")
        # 2026-11-25 ECS 实测：Issue #507 两条触发语句（CREATE GLOBAL INDEX / 复制表 CREATE TRIGGER）均报 XX000 → 升级为 reproduced
        assert SQLSTATE_TABLE["XX000"]["otb_note"].startswith("reproduced")


class TestDiagnose:
    def test_sqlstate_known(self):
        d = diagnostics.diagnose_sqlstate("42704")
        assert d["matched"] is True
        assert d["name"] == "undefined_object"
        assert d["confidence"] == "confirmed"
        # 42704 是名称解析失败类别：多原因并列，不得把单一实测案例当作通用根因
        assert len(d["possible_causes"]) >= 3
        assert any("search_path" in c for c in d["possible_causes"])
        assert any("PGXC" in c for c in d["possible_causes"])
        # 只有 SQLSTATE 时必须表达不确定性
        assert "错误类别" in d["conclusion"]
        assert "不同原因" in d["conclusion"]

    def test_sqlstate_unknown_honest(self):
        d = diagnostics.diagnose_sqlstate("0A999")
        assert d["matched"] is False
        assert "不在当前案例库中" in d["conclusion"]

    def test_text_known(self):
        d = diagnostics.diagnose_text("error while loading shared libraries: libssl.so.1.1: cannot open shared object file")
        assert d["matched"] is True
        assert d["matches"][0]["id"] == "dynlib-missing"

    def test_text_unknown(self):
        d = diagnostics.diagnose_text("weird unknown error text")
        assert d["matched"] is False

    def test_text_full_refused_specific_covers_generic(self):
        # 完整 psql 错误文本命中具体签名；同症状通用签名被覆盖，不重复输出
        d = diagnostics.diagnose_text("psql: could not connect to server: Connection refused")
        assert d["matched"] is True
        ids = [m["id"] for m in d["matches"]]
        assert "connection-refused" in ids
        assert "connection-refused-generic" not in ids
        assert d["confidence"] == "confirmed"

    def test_text_bare_refused_generic_candidate(self):
        # 裸文本只识别错误类别，置信度为 candidate，且不把节点宕机当作确定结论
        d = diagnostics.diagnose_text("Connection refused")
        assert d["matched"] is True
        assert d["confidence"] == "candidate"
        assert d["matches"][0]["id"] == "connection-refused-generic"
        assert any("需结合端口监听状态与实际连接串区分" in c for c in d["matches"][0]["possible_causes"])

    def test_text_type_not_exists_causes_cover_search_path(self):
        d = diagnostics.diagnose_text('ERROR:  (42704) type "memories" does not exist')
        assert d["matched"] is True
        m = next(x for x in d["matches"] if x["id"] == "type-not-exists")
        assert m["matched_fields"]["name"] == "memories"
        assert any("search_path" in c for c in m["possible_causes"])
        # 不得由该错误推断「对象是表而非类型」；普通表自动创建同名复合类型
        assert any("复合类型" in s for s in m["next_steps"])

    def test_diagnose_logs_with_fixtures(self, monkeypatch):
        from datetime import datetime, timezone, timedelta

        monkeypatch.setattr(
            log_parser, "now_utc", lambda: datetime(2026, 11, 21, tzinfo=timezone.utc)
        )

        d = diagnostics.diagnose_logs(
            cn_paths=[os.path.join(FIX, "logs", "cn_sample.log")],
            dn_paths=[os.path.join(FIX, "logs", "dn1_sample.log"), os.path.join(FIX, "logs", "dn2_sample.log")],
            window=timedelta(days=30),
        )
        assert d["error_fatal_in_window"] == 8
        assert "node_state" in d["category_counts"] or "distribution" in d["category_counts"]
        confirmed = [c for c in d["correlations"] if c["confidence"] == "confirmed"]
        assert any(c["coord"] == "2533,76" for c in confirmed)
        # 日志关联识别跨节点异常事件，错误签名提供可能原因及排查建议，均不直接下根因结论
        assert d["root_cause"]["determined"] is False
        assert "日志关联用于识别跨节点异常事件" in d["root_cause"]["note"]
        assert "错误签名用于提供可能原因及排查建议" in d["root_cause"]["note"]

    def test_diagnose_logs_missing_files(self):
        from datetime import timedelta

        d = diagnostics.diagnose_logs(
            cn_paths=[os.path.join(FIX, "logs", "missing.log")],
            dn_paths=[],
            window=timedelta(days=1),
        )
        assert len(d["read_errors"]) == 1
        assert "证据不完整" in d["conclusion"]

    def test_diagnose_logs_all_unparsable_reports_format_mismatch(self, tmp_path):
        from datetime import timedelta

        path = tmp_path / "garbage.log"
        path.write_text("not a log line\nanother garbage line\n")
        d = diagnostics.diagnose_logs(
            cn_paths=[str(path)],
            dn_paths=[],
            window=timedelta(days=1),
        )
        assert d["unparsed_files"] == [str(path)]
        assert "格式与解析器不匹配" in d["conclusion"]

    def test_diagnose_logs_blank_only_file_not_mismatch(self, tmp_path):
        from datetime import timedelta

        path = tmp_path / "blank.log"
        path.write_text("\n\n\n")
        d = diagnostics.diagnose_logs(
            cn_paths=[str(path)],
            dn_paths=[],
            window=timedelta(days=1),
        )
        assert d["unparsed_files"] == []
        assert d["unparsed_lines"] == 0

    def test_diagnose_logs_partial_read_no_false_negative(self, tmp_path, monkeypatch):
        # 部分文件可读且含错误时结论表述为「可解析部分发现」，不得因另有文件缺失而吞掉
        from datetime import datetime, timedelta, timezone

        monkeypatch.setattr(
            log_parser, "now_utc", lambda: datetime(2026, 11, 21, tzinfo=timezone.utc)
        )

        good = tmp_path / "good.log"
        good.write_text(
            "2026-11-20 17:43:54.411 CST [2545,coord(2533,76)] FATAL:  terminating connection due to administrator command\n"
        )
        d = diagnostics.diagnose_logs(
            cn_paths=[str(good), str(tmp_path / "missing.log")],
            dn_paths=[],
            window=timedelta(days=30),
        )
        assert len(d["read_errors"]) == 1
        assert "可解析部分发现" in d["conclusion"]
        assert "时间窗口内未发现 ERROR/FATAL" not in d["conclusion"]

    def test_diagnose_logs_incomplete_no_false_negative(self, tmp_path, monkeypatch):
        # 存在读取失败且可解析部分无错误：不得输出无条件「未发现 ERROR/FATAL」
        from datetime import datetime, timedelta, timezone

        monkeypatch.setattr(
            log_parser, "now_utc", lambda: datetime(2026, 11, 21, tzinfo=timezone.utc)
        )

        good = tmp_path / "good.log"
        good.write_text("2026-11-20 17:43:54.411 CST [2545,coord(0,0)] LOG:  InitMultinodeExecutor force:0.\n")
        d = diagnostics.diagnose_logs(
            cn_paths=[str(good), str(tmp_path / "missing.log")],
            dn_paths=[],
            window=timedelta(days=30),
        )
        assert len(d["read_errors"]) == 1
        assert "时间窗口内未发现 ERROR/FATAL" not in d["conclusion"]

    def test_correlation_truncation_keeps_confirmed(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        from otb_tools.log_parser import LogEntry

        now = datetime.now(timezone.utc)
        cn, dn = tmp_path / "cn.log", tmp_path / "dn.log"
        lines = []
        for i in range(25):
            ts = (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            lines.append(f"{ts} UTC [{1000 + i},coord({1000 + i},0)] LOG:  some log message\n")
        # 字典序最大的键才是 confirmed，验证排序后不被 [:20] 截断挤掉
        ts_c = (now - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        ts_d = (now - timedelta(seconds=4)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        lines.append(f"{ts_c} UTC [9999,coord(9999,1)] LOG:  Changing connection state for node dn0001 pid: 9000, state ERROR ==> FATAL, invoker execRemote.c: 862\n")
        lines.append(f"{ts_d} UTC [9000,coord(9999,1)] FATAL:  terminating connection due to administrator command\n")
        cn.write_text("\n".join(lines[:25]) + "\n" + lines[25] + "\n")
        dn.write_text(lines[26] + "\n")

        d = diagnostics.diagnose_logs(
            cn_paths=[str(cn)],
            dn_paths=[str(dn)],
            window=timedelta(hours=1),
        )
        assert len(d["correlations"]) == 20
        assert any(c["coord"] == "9999,1" and c["confidence"] == "confirmed" for c in d["correlations"])


class TestPreflightUnit:
    def _cfg(self, tmp_path, monkeypatch):
        from otb_tools.preflight import PreflightConfig

        cfg = PreflightConfig(
            pg_bin=str(tmp_path / "bin"),
            runtime_libpq=str(tmp_path / "runtime" / "libpq"),
            compat_dirs=[],
            data_dirs=[str(tmp_path / "data")],
            log_dir=str(tmp_path / "logs"),
            ports=[5000],
            conf_files=[],
            pg_hba_files=[],
        )
        os.makedirs(cfg.pg_bin, exist_ok=True)
        os.makedirs(cfg.data_dirs[0], exist_ok=True)
        os.makedirs(cfg.log_dir, exist_ok=True)
        for tool in ["postgres", "psql", "gtm_ctl", "pg_ctl"]:
            p = os.path.join(cfg.pg_bin, tool)
            with open(p, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(p, 0o755)
        monkeypatch.setattr(net, "ss_listening_ports", lambda: {15401})
        return cfg

    def test_env_var_missing_warns(self, tmp_path, monkeypatch):
        from otb_tools.preflight import PreflightConfig, _check_env_ld_library_path

        monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
        item = _check_env_ld_library_path(PreflightConfig(compat_dirs=[]))
        assert item.status == Status.WARN
        assert "env.sh" in item.fix_hint

    def test_env_var_ok(self, tmp_path, monkeypatch):
        from otb_tools.preflight import PreflightConfig, _check_env_ld_library_path

        monkeypatch.setenv(
            "LD_LIBRARY_PATH",
            "/opt/opentenbase-lab/runtime/libpq:/opt/opentenbase-lab/compat/usr/lib/x86_64-linux-gnu:/opt/opentenbase-lab/compat/lib/x86_64-linux-gnu",
        )
        item = _check_env_ld_library_path(PreflightConfig())
        assert item.status == Status.PASS

    def test_ldd_missing_lib_fails(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_ldd

        cfg = self._cfg(tmp_path, monkeypatch)
        monkeypatch.setattr(preflight, "_run", lambda cmd, timeout=10: (
            0, "\tlibreadline.so.7 => not found\n\tlibssl.so.1.1 => /x/libssl.so.1.1", ""
        ))
        item = _check_ldd(cfg)
        assert item.status == Status.FAIL
        assert any("libreadline" in e for e in item.evidence)

    def test_ldd_ok(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_ldd

        cfg = self._cfg(tmp_path, monkeypatch)
        monkeypatch.setattr(preflight, "_run", lambda cmd, timeout=10: (0, "\tlibssl.so.1.1 => /x/libssl.so.1.1", ""))
        assert _check_ldd(cfg).status == Status.PASS

    def test_libpq_symlink_mismatch(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_libpq_symlink

        cfg = self._cfg(tmp_path, monkeypatch)
        link = os.path.join(cfg.runtime_libpq, "libpq.so.5")
        target = os.path.join(cfg.pg_bin, "..", "lib", "libpq.so.5.10")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.makedirs(cfg.runtime_libpq, exist_ok=True)
        with open(target, "w") as fh:
            fh.write("")
        with open(link, "w") as fh:
            fh.write("")  # 普通文件而非正确符号链接
        item = _check_libpq_symlink(cfg)
        assert item.status == Status.FAIL

    def test_ports_preflight_mode(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_ports

        cfg = self._cfg(tmp_path, monkeypatch)
        monkeypatch.setattr(net, "ss_listening_ports", lambda: set())
        item = _check_ports(cfg)  # expect_running=False
        assert item.status == Status.PASS  # 端口空闲可部署
        cfg.expect_running = True
        item = _check_ports(cfg)
        assert item.status == Status.FAIL  # 期望运行但未监听

    def test_duplicate_guc_detected(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_duplicate_guc

        cfg = self._cfg(tmp_path, monkeypatch)
        conf = tmp_path / "postgresql.conf"
        conf.write_text(
            "# comment\nmax_connections = 100\nshared_buffers = 128MB\nmax_connections = 80\n"
        )
        cfg.conf_files = [str(conf)]
        item = _check_duplicate_guc(cfg)
        assert item.status == Status.WARN
        assert any("max_connections" in e for e in item.evidence)

    def test_no_duplicate_guc(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_duplicate_guc

        cfg = self._cfg(tmp_path, monkeypatch)
        conf = tmp_path / "postgresql.conf"
        conf.write_text("max_connections = 100\nshared_buffers = 128MB\n")
        cfg.conf_files = [str(conf)]
        assert _check_duplicate_guc(cfg).status == Status.PASS

    def test_pg_hba_trust_warns(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_pg_hba_trust

        cfg = self._cfg(tmp_path, monkeypatch)
        hba = tmp_path / "pg_hba.conf"
        hba.write_text(
            "# comment\nlocal   all   all   trust\nhost    all   all   127.0.0.1/32   md5\n"
        )
        cfg.pg_hba_files = [str(hba)]
        item = _check_pg_hba_trust(cfg)
        assert item.status == Status.WARN
        assert any("trust" in e for e in item.evidence)

    def test_pg_hba_auth_field_handles_comments_quotes_options_and_netmask(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_pg_hba_trust

        cfg = self._cfg(tmp_path, monkeypatch)
        hba = tmp_path / "pg_hba.conf"
        hba.write_text(
            '# full-line comment\n'
            'local "trust" all scram-sha-256\n'
            'local "db#name" "user name" trust # quoted hash before method\n'
            "local all o'brien trust # method followed by comment\n"
            'host "trust" all 127.0.0.1/32 scram-sha-256\n'
            'host all all 127.0.0.1/32 md5 map="trust # option" # trailing comment\n'
            'hostssl all all 127.0.0.1/32 trust clientcert=1 # auth option\n'
            'host all all 127.0.0.1 255.255.255.255 trust # legacy netmask\n'
        )
        cfg.pg_hba_files = [str(hba)]
        item = _check_pg_hba_trust(cfg)
        assert item.status == Status.WARN
        assert item.summary.startswith("4 条 trust 规则")
        assert any('db#name' in e and 'trust' in e for e in item.evidence)
        assert any("o'brien trust" in e for e in item.evidence)
        assert any("255.255.255.255 trust" in e for e in item.evidence)

    def test_db_probe_broken_env_127(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_db_probe

        cfg = self._cfg(tmp_path, monkeypatch)
        cfg.conninfo = "host=127.0.0.1 port=15001 user=student password=secret"
        monkeypatch.setattr(
            preflight,
            "_run",
            lambda cmd, env=None, timeout=10: (
                127,
                "",
                "psql: error while loading shared libraries: libreadline.so.7: cannot open shared object file",
            ),
        )
        item = _check_db_probe(cfg)
        assert item.status == Status.FAIL
        assert "exit_code=127" in item.evidence[0]
        assert any("LD_LIBRARY_PATH" in c for c in item.causes)

    def test_db_probe_psql_not_found_distinguished(self, tmp_path, monkeypatch):
        # 回归：psql 不在 PATH（FileNotFoundError→127）不得误报为动态库缺失
        from otb_tools.preflight import _check_db_probe

        cfg = self._cfg(tmp_path, monkeypatch)
        cfg.conninfo = "host=127.0.0.1 port=15001 user=student"
        monkeypatch.setattr(
            preflight,
            "_run",
            lambda cmd, env=None, timeout=10: (127, "", "[Errno 2] No such file or directory: 'psql'"),
        )
        item = _check_db_probe(cfg)
        assert item.status == Status.FAIL
        assert "PATH" in item.summary
        assert any("env.sh" in c or "PATH" in c for c in item.causes)
        assert "动态库" not in item.summary

    def test_db_probe_refused_predeploy_is_warn(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_db_probe

        cfg = self._cfg(tmp_path, monkeypatch)
        cfg.conninfo = "host=127.0.0.1 port=15001"
        monkeypatch.setattr(
            preflight, "_run",
            lambda cmd, env=None, timeout=10: (2, "", "psql: could not connect to server: Connection refused"),
        )
        assert _check_db_probe(cfg).status == Status.WARN

    def test_db_probe_refused_expect_running_fails(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_db_probe

        cfg = self._cfg(tmp_path, monkeypatch)
        cfg.conninfo = "host=127.0.0.1 port=15001"
        cfg.expect_running = True
        monkeypatch.setattr(
            preflight, "_run",
            lambda cmd, env=None, timeout=10: (2, "", "psql: could not connect to server: Connection refused"),
        )
        assert _check_db_probe(cfg).status == Status.FAIL

    def test_db_probe_password_redacted(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_db_probe

        cfg = self._cfg(tmp_path, monkeypatch)
        cfg.conninfo = "host=127.0.0.1 port=15001 user=student password=secret"
        monkeypatch.setattr(preflight, "_run", lambda cmd, env=None, timeout=10: (0, "1", ""))
        item = _check_db_probe(cfg)
        assert "secret" not in item.source[0]
        assert "***" in item.source[0]

    def test_db_probe_prefers_pg_bin_psql(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_db_probe

        captured = {}
        cfg = self._cfg(tmp_path, monkeypatch)
        cfg.conninfo = "host=127.0.0.1 port=15001 user=student"

        def fake_run(cmd, env=None, timeout=10):
            captured["cmd"] = cmd
            return (0, "1", "")

        monkeypatch.setattr(preflight, "_run", fake_run)
        item = _check_db_probe(cfg)
        assert item.status == Status.PASS
        assert captured["cmd"][0] == os.path.join(cfg.pg_bin, "psql")
        assert captured["cmd"][1] == f"--dbname={cfg.conninfo}"
        assert item.source[0].startswith(os.path.join(cfg.pg_bin, "psql"))

    def test_db_probe_does_not_treat_psql_version_output_as_success(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_db_probe

        cfg = self._cfg(tmp_path, monkeypatch)
        cfg.conninfo = "--version"
        captured = {}

        def fake_run(cmd, env=None, timeout=10):
            captured["cmd"] = cmd
            # Models psql's option behavior if --version were not bound as dbname.
            return (0, "psql (PostgreSQL) 18.6", "")

        monkeypatch.setattr(preflight, "_run", fake_run)
        item = _check_db_probe(cfg)
        assert captured["cmd"][1] == "--dbname=--version"
        assert item.status == Status.FAIL
        assert "未返回预期探测结果" in item.summary

    @pytest.mark.parametrize("conninfo", [
        "host=127.0.0.1 port=15001 dbname=lab user=student",
        "postgresql://student:p%40ss@127.0.0.1:15001/lab",
    ])
    def test_db_probe_binds_keyword_and_uri_conninfo(self, tmp_path, monkeypatch, conninfo):
        from otb_tools.preflight import _check_db_probe

        cfg = self._cfg(tmp_path, monkeypatch)
        cfg.conninfo = conninfo
        captured = {}

        def fake_run(cmd, env=None, timeout=10):
            captured["cmd"] = cmd
            return (0, "1", "")

        monkeypatch.setattr(preflight, "_run", fake_run)
        item = _check_db_probe(cfg)
        assert item.status == Status.PASS
        assert captured["cmd"][1] == f"--dbname={conninfo}"

    def test_db_probe_falls_back_to_path_psql(self, tmp_path, monkeypatch):
        from otb_tools.preflight import _check_db_probe

        captured = {}
        cfg = self._cfg(tmp_path, monkeypatch)
        os.remove(os.path.join(cfg.pg_bin, "psql"))
        cfg.conninfo = "host=127.0.0.1 port=15001"

        def fake_run(cmd, env=None, timeout=10):
            captured["cmd"] = cmd
            return (2, "", "psql: could not connect to server: Connection refused")

        monkeypatch.setattr(preflight, "_run", fake_run)
        item = _check_db_probe(cfg)
        assert item.status == Status.WARN
        assert captured["cmd"][0] == "psql"
