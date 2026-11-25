"""contrib-standalone/otb_preflight.py 的单测。

standalone 版是零第三方依赖的单文件脚本，面向 OpenTenBase 主仓库 contrib/ 提交。
测试仅用 pytest + monkeypatch，覆盖全部检查项、退出码语义与凭证脱敏（Codex 轮
修复的密码泄漏点在此版同步锁定）。
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
STANDALONE = os.path.join(HERE, "..", "contrib-standalone")
sys.path.insert(0, STANDALONE)

import otb_preflight as sf  # noqa: E402


class Cfg:
    def __init__(self, tmp_path, **kw):
        self.pg_bin = str(tmp_path / "bin")
        self.runtime_libpq = str(tmp_path / "runtime" / "libpq")
        self.compat_dirs = []
        self.data_dirs = [str(tmp_path / "data")]
        self.log_dir = str(tmp_path / "logs")
        self.ports = [5000]
        self.conf_files = []
        self.pg_hba_files = []
        self.conn = ""
        self.expect_running = False
        self.probe_env = None
        self.disk_min_free_gb = 1.0
        self.timeout = 10
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture
def cfg(tmp_path):
    c = Cfg(tmp_path)
    os.makedirs(c.pg_bin, exist_ok=True)
    os.makedirs(c.data_dirs[0], exist_ok=True)
    os.makedirs(c.log_dir, exist_ok=True)
    return c


@pytest.fixture
def cfg_with_tools(cfg):
    for tool in ["postgres", "psql", "gtm_ctl", "pg_ctl"]:
        p = os.path.join(cfg.pg_bin, tool)
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(p, 0o755)
    return cfg


class TestSignatures:
    @pytest.mark.parametrize(
        "text,expected_id",
        [
            ("psql: error while loading shared libraries: libreadline.so.7: cannot open shared object file", "dynlib-missing"),
            ("psql: could not connect to server: Connection refused", "connection-refused"),
            ('FATAL:  role "nobody" does not exist', "role-not-exists"),
            ('FATAL:  database "nodb" does not exist', "database-not-exists"),
            ("ERROR:  PGXC Group grp: group not defined", "pgxc-group-not-defined"),
            ("ERROR:  default group not defined", "default-group-not-defined"),
            ("FATAL:  the database system is in recovery mode", "recovery-mode"),
            ("FATAL:  remaining connection slots are reserved", "too-many-connections"),
        ],
    )
    def test_signature_hits(self, text, expected_id):
        hits = sf._match_signatures(text)
        assert any(h["id"] == expected_id for h in hits)

    def test_unknown_text_no_hit(self):
        assert sf._match_signatures("something completely unfamiliar") == []


class TestEnvLdLibraryPath:
    def test_unset_warns(self, cfg, monkeypatch):
        monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
        item = sf.check_env_ld_library_path(cfg)
        assert item.status == sf.STATUS_WARN
        assert "env.sh" in item.fix_hint

    def test_set_but_missing_compat_dir_warns(self, cfg, monkeypatch):
        monkeypatch.setenv("LD_LIBRARY_PATH", "/only/runtime/libpq")
        cfg.compat_dirs = ["/opt/compat/lib"]
        item = sf.check_env_ld_library_path(cfg)
        assert item.status == sf.STATUS_WARN

    def test_set_complete_passes(self, cfg, monkeypatch):
        monkeypatch.setenv("LD_LIBRARY_PATH", "/r/libpq:/c/usr/lib:/c/lib")
        cfg.compat_dirs = ["/c/usr/lib", "/c/lib"]
        assert sf.check_env_ld_library_path(cfg).status == sf.STATUS_PASS


class TestBinaries:
    def test_all_present(self, cfg_with_tools):
        item = sf.check_binaries(cfg_with_tools)
        assert item.status == sf.STATUS_PASS

    def test_missing_tool_fails(self, cfg_with_tools):
        os.remove(os.path.join(cfg_with_tools.pg_bin, "gtm_ctl"))
        item = sf.check_binaries(cfg_with_tools)
        assert item.status == sf.STATUS_FAIL


class TestLdd:
    def test_missing_lib_fails(self, cfg_with_tools, monkeypatch):
        monkeypatch.setattr(
            sf, "_run", lambda cmd, env=None, timeout=10: (0, "\tlibreadline.so.7 => not found", "")
        )
        item = sf.check_ldd(cfg_with_tools)
        assert item.status == sf.STATUS_FAIL
        assert any("libreadline" in e for e in item.evidence)

    def test_all_resolved_passes(self, cfg_with_tools, monkeypatch):
        monkeypatch.setattr(
            sf, "_run", lambda cmd, env=None, timeout=10: (0, "\tlibssl.so.1.1 => /x/libssl.so.1.1", "")
        )
        assert sf.check_ldd(cfg_with_tools).status == sf.STATUS_PASS

    def test_no_postgres_skips(self, cfg):
        assert sf.check_ldd(cfg).status == sf.STATUS_SKIP


class TestLibpqSymlink:
    def test_mismatch_fails(self, cfg_with_tools):
        link = os.path.join(cfg_with_tools.runtime_libpq, "libpq.so.5")
        target = os.path.join(cfg_with_tools.pg_bin, "..", "lib", "libpq.so.5.10")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.makedirs(cfg_with_tools.runtime_libpq, exist_ok=True)
        with open(target, "w") as fh:
            fh.write("")
        with open(link, "w") as fh:
            fh.write("")
        item = sf.check_libpq_symlink(cfg_with_tools)
        assert item.status == sf.STATUS_FAIL

    def test_correct_symlink_passes(self, cfg_with_tools):
        link = os.path.join(cfg_with_tools.runtime_libpq, "libpq.so.5")
        target = os.path.join(cfg_with_tools.pg_bin, "..", "lib", "libpq.so.5.10")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.makedirs(cfg_with_tools.runtime_libpq, exist_ok=True)
        with open(target, "w") as fh:
            fh.write("")
        os.symlink(os.path.realpath(target), link)
        assert sf.check_libpq_symlink(cfg_with_tools).status == sf.STATUS_PASS

    def test_missing_paths_skip(self, cfg):
        assert sf.check_libpq_symlink(cfg).status == sf.STATUS_SKIP


class TestPorts:
    def test_predeploy_free_passes(self, cfg, monkeypatch):
        monkeypatch.setattr(sf, "_ss_listening_ports", lambda: set())
        assert sf.check_ports(cfg).status == sf.STATUS_PASS

    def test_predeploy_occupied_warns(self, cfg, monkeypatch):
        monkeypatch.setattr(sf, "_ss_listening_ports", lambda: {5000})
        assert sf.check_ports(cfg).status == sf.STATUS_WARN

    def test_expect_running_not_listening_fails(self, cfg, monkeypatch):
        monkeypatch.setattr(sf, "_ss_listening_ports", lambda: set())
        cfg.expect_running = True
        item = sf.check_ports(cfg)
        assert item.status == sf.STATUS_FAIL
        assert any("未启动" in c for c in item.causes)

    def test_ss_unavailable_errors(self, cfg, monkeypatch):
        monkeypatch.setattr(sf, "_ss_listening_ports", lambda: (_ for _ in ()).throw(OSError("ss 不可用: no ss")))
        assert sf.check_ports(cfg).status == sf.STATUS_ERROR


class TestDirs:
    def test_all_exist(self, cfg):
        assert sf.check_dirs(cfg).status == sf.STATUS_PASS

    def test_missing_fails(self, cfg):
        cfg.data_dirs.append(str(os.path.dirname(cfg.log_dir)) + "/nonexistent")
        assert sf.check_dirs(cfg).status == sf.STATUS_FAIL


class TestDisk:
    def test_enough_space(self, cfg, monkeypatch):
        monkeypatch.setattr(sf.shutil, "disk_usage", lambda p: type("U", (), {"free": 100 * 1024**3, "total": 200 * 1024**3})())
        assert sf.check_disk(cfg).status == sf.STATUS_PASS

    def test_low_space_warns(self, cfg, monkeypatch):
        monkeypatch.setattr(sf.shutil, "disk_usage", lambda p: type("U", (), {"free": 0.1 * 1024**3, "total": 200 * 1024**3})())
        assert sf.check_disk(cfg).status == sf.STATUS_WARN

    def test_disk_error(self, cfg, monkeypatch):
        monkeypatch.setattr(sf.shutil, "disk_usage", lambda p: (_ for _ in ()).throw(OSError("nope")))
        assert sf.check_disk(cfg).status == sf.STATUS_ERROR


class TestDuplicateGuc:
    def test_no_conf_skips(self, cfg):
        assert sf.check_duplicate_guc(cfg).status == sf.STATUS_SKIP

    def test_duplicate_warns(self, cfg):
        conf = os.path.join(cfg.log_dir, "postgresql.conf")
        with open(conf, "w") as fh:
            fh.write("max_connections = 100\nmax_connections = 80\n")
        cfg.conf_files = [conf]
        item = sf.check_duplicate_guc(cfg)
        assert item.status == sf.STATUS_WARN
        assert any("max_connections" in e for e in item.evidence)

    def test_no_duplicate_passes(self, cfg):
        conf = os.path.join(cfg.log_dir, "postgresql.conf")
        with open(conf, "w") as fh:
            fh.write("max_connections = 100\nshared_buffers = 128MB\n")
        cfg.conf_files = [conf]
        assert sf.check_duplicate_guc(cfg).status == sf.STATUS_PASS

    def test_unreadable_errors(self, cfg):
        cfg.conf_files = [os.path.join(cfg.log_dir, "missing.conf")]
        assert sf.check_duplicate_guc(cfg).status == sf.STATUS_ERROR


class TestPgHbaTrust:
    def test_no_hba_skips(self, cfg):
        assert sf.check_pg_hba_trust(cfg).status == sf.STATUS_SKIP

    def test_trust_warns(self, cfg):
        hba = os.path.join(cfg.log_dir, "pg_hba.conf")
        with open(hba, "w") as fh:
            fh.write("local   all   all   trust\nhost    all   all   127.0.0.1/32   md5\n")
        cfg.pg_hba_files = [hba]
        item = sf.check_pg_hba_trust(cfg)
        assert item.status == sf.STATUS_WARN
        assert any("trust" in e for e in item.evidence)

    def test_no_trust_passes(self, cfg):
        hba = os.path.join(cfg.log_dir, "pg_hba.conf")
        with open(hba, "w") as fh:
            fh.write("host    all   all   127.0.0.1/32   md5\n")
        cfg.pg_hba_files = [hba]
        assert sf.check_pg_hba_trust(cfg).status == sf.STATUS_PASS


class TestDbProbe:
    def test_no_conn_skips(self, cfg):
        assert sf.check_db_probe(cfg).status == sf.STATUS_SKIP

    def test_connect_ok(self, cfg, monkeypatch):
        cfg.conn = "host=127.0.0.1 port=15001 user=student"
        monkeypatch.setattr(sf, "_run", lambda cmd, env=None, timeout=10: (0, "1", ""))
        item = sf.check_db_probe(cfg)
        assert item.status == sf.STATUS_PASS

    def test_dynlib_missing_127(self, cfg, monkeypatch):
        cfg.conn = "host=127.0.0.1 port=15001"
        monkeypatch.setattr(
            sf,
            "_run",
            lambda cmd, env=None, timeout=10: (
                127, "", "psql: error while loading shared libraries: libreadline.so.7: cannot open shared object file",
            ),
        )
        item = sf.check_db_probe(cfg)
        assert item.status == sf.STATUS_FAIL
        assert "动态库缺失" in item.summary
        assert any("LD_LIBRARY_PATH" in c for c in item.causes)
        assert "env.sh" in item.fix_hint

    def test_psql_not_found_distinguished(self, cfg, monkeypatch):
        # 回归：psql 不在 PATH（FileNotFoundError→127）不得误报为动态库缺失
        cfg.conn = "host=127.0.0.1 port=15001"
        monkeypatch.setattr(
            sf, "_run",
            lambda cmd, env=None, timeout=10: (127, "", "[Errno 2] No such file or directory: 'psql'"),
        )
        item = sf.check_db_probe(cfg)
        assert item.status == sf.STATUS_FAIL
        assert "PATH" in item.summary
        assert "动态库" not in item.summary

    def test_refused_predeploy_warns(self, cfg, monkeypatch):
        cfg.conn = "host=127.0.0.1 port=15001"
        monkeypatch.setattr(
            sf, "_run",
            lambda cmd, env=None, timeout=10: (2, "", "psql: could not connect to server: Connection refused"),
        )
        assert sf.check_db_probe(cfg).status == sf.STATUS_WARN

    def test_refused_expect_running_fails(self, cfg, monkeypatch):
        cfg.conn = "host=127.0.0.1 port=15001"
        cfg.expect_running = True
        monkeypatch.setattr(
            sf, "_run",
            lambda cmd, env=None, timeout=10: (2, "", "psql: could not connect to server: Connection refused"),
        )
        assert sf.check_db_probe(cfg).status == sf.STATUS_FAIL

    def test_signature_causes_attached(self, cfg, monkeypatch):
        cfg.conn = "host=127.0.0.1 port=15001"
        monkeypatch.setattr(
            sf, "_run",
            lambda cmd, env=None, timeout=10: (2, "", 'psql: FATAL:  role "nobody" does not exist'),
        )
        item = sf.check_db_probe(cfg)
        assert any("连接用户" in c for c in item.causes)

    def test_probe_env_password_redacted(self, cfg, monkeypatch):
        cfg.conn = "host=127.0.0.1 port=15001"
        cfg.probe_env = ["PGPASSWORD=secret", "LD_LIBRARY_PATH=/wrong"]
        monkeypatch.setattr(sf, "_run", lambda cmd, env=None, timeout=10: (2, "", "Connection refused"))
        item = sf.check_db_probe(cfg)
        joined = "\n".join(item.evidence)
        assert "secret" not in joined
        assert "PGPASSWORD=***" in joined

    def test_timeout_message_no_cmd_echo(self, cfg, monkeypatch):
        # 回归：超时消息不得回显完整命令行（可能含密码）
        cfg.conn = "host=127.0.0.1 port=15001 password=secret"
        monkeypatch.setattr(
            sf, "_run",
            lambda cmd, env=None, timeout=10: (124, "", "command timed out after 10s"),
        )
        item = sf.check_db_probe(cfg)
        assert item.status == sf.STATUS_FAIL
        assert "secret" not in "\n".join(item.evidence)
        assert "password" not in "\n".join(item.evidence)


class TestRedactConninfo:
    @pytest.mark.parametrize(
        "raw,marker",
        [
            ("host=h password='secret with spaces'", "'***'"),
            ('host=h password="secret" user=u', '"***"'),
            ("host=h password=plain", "***"),
            ("postgresql://user:secret@host:5432/db", "postgresql://user:***@host:5432/db"),
            ("postgresql://user@host/db?password=secret&x=1", "password=%2A%2A%2A"),
            ("postgresql://user:secret@host/db?password=q&sslpassword=s", None),
        ],
    )
    def test_redaction(self, raw, marker):
        out = sf._redact_conninfo(raw)
        assert "secret" not in out
        if marker:
            assert marker in out
        # sslpassword 也脱敏
        if "sslpassword" in raw:
            assert "***" in out


class TestMainExitCodes:
    def _all_pass_env(self, cfg, monkeypatch):
        monkeypatch.setattr(sf, "_ss_listening_ports", lambda: set())
        monkeypatch.setattr(sf.shutil, "disk_usage", lambda p: type("U", (), {"free": 100 * 1024**3, "total": 200 * 1024**3})())
        return [
            "--pg-bin", cfg.pg_bin,
            "--runtime-libpq", cfg.runtime_libpq,
            "--data-dirs", cfg.data_dirs[0],
            "--log-dir", cfg.log_dir,
            "--ports", "5000",
            "--compat-dirs", "",
        ]

    def test_all_pass_exit0(self, cfg_with_tools, monkeypatch, capsys):
        argv = self._all_pass_env(cfg_with_tools, monkeypatch)
        assert sf.main(argv) == 0
        assert "exit_code=0" in capsys.readouterr().out

    def test_fail_exit1(self, cfg_with_tools, monkeypatch, capsys):
        argv = self._all_pass_env(cfg_with_tools, monkeypatch)
        os.remove(os.path.join(cfg_with_tools.pg_bin, "psql"))
        assert sf.main(argv) == 1

    def test_error_exit2(self, cfg, monkeypatch):
        # ss 不可用 → port.check ERROR → 退出码 2
        monkeypatch.setattr(sf, "_ss_listening_ports", lambda: (_ for _ in ()).throw(OSError("ss 不可用")))
        monkeypatch.setattr(sf.shutil, "disk_usage", lambda p: type("U", (), {"free": 100 * 1024**3, "total": 200 * 1024**3})())
        argv = [
            "--pg-bin", cfg.pg_bin, "--runtime-libpq", cfg.runtime_libpq,
            "--data-dirs", cfg.data_dirs[0], "--log-dir", cfg.log_dir,
            "--ports", "5000", "--compat-dirs", "",
        ]
        assert sf.main(argv) == 2

    def test_arg_error_exit3(self, capsys):
        with pytest.raises(SystemExit) as exc:
            sf.main(["--no-such-option"])
        assert exc.value.code == 3

    def test_conn_redacted_in_header(self, cfg_with_tools, monkeypatch, capsys):
        argv = self._all_pass_env(cfg_with_tools, monkeypatch)
        argv += ["--conn", "host=h password=secret"]
        monkeypatch.setattr(sf, "_run", lambda cmd, env=None, timeout=10: (0, "1", ""))
        sf.main(argv)
        out = capsys.readouterr().out
        assert "secret" not in out
        assert "password=***" in out
