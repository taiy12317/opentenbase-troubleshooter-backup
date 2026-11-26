import os
from datetime import timedelta

import pytest

from otb_tools import log_parser

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "logs")


class TestParseLine:
    def test_valid_line(self):
        e = log_parser.parse_line(
            "2026-11-20 17:43:54.411 CST [2545,coord(2533,76)] FATAL:  terminating connection due to administrator command",
            node="dn1",
            source="dn1.log",
            lineno=3,
        )
        assert e is not None
        assert e.pid == 2545
        assert e.coord_pid == 2533
        assert e.coord_vxid == 76
        assert e.level == "FATAL"
        assert e.node == "dn1"
        assert e.coord_key() == "2533,76"
        assert "admin" in e.message

    def test_sqlstate_extracted(self):
        e = log_parser.parse_line(
            '2026-11-20 18:02:00.000 CST [2901,coord(2901,6)] ERROR:  (42704) PGXC Group nonexist_group: group not defined'
        )
        assert e.sqlstate == "42704"
        assert "distribution" in e.categories

    def test_internal_event_has_no_key(self):
        e = log_parser.parse_line(
            "2026-11-20 17:43:54.388 CST [2545,coord(0,0)] LOG:  InitMultinodeExecutor force:0."
        )
        assert e.coord_key() is None

    def test_cst_converted_to_utc(self):
        e = log_parser.parse_line(
            "2026-11-20 17:43:54.411 CST [2545,coord(2533,76)] FATAL:  terminating connection due to administrator command"
        )
        assert e.ts.tzinfo is not None
        assert e.ts.utcoffset() == timedelta(0)
        assert e.ts.hour == 9  # 17:43 CST → 09:43 UTC
        # 窗口过滤基于 UTC：解析值与 now_utc() 可直接比较
        assert isinstance(e.ts - log_parser.now_utc(), timedelta)

    def test_unknown_tz_treated_as_utc(self):
        e = log_parser.parse_line(
            "2026-11-20 17:43:54.411 XYZ [2545,coord(2533,76)] FATAL:  x"
        )
        assert e.ts.utcoffset() == timedelta(0)

    def test_malformed_returns_none(self):
        assert log_parser.parse_line("not-a-valid-log-line just text") is None
        assert log_parser.parse_line("2026-13-99 bad ts [1,coord(1,1)] LOG: x") is None


class TestParseFile:
    def test_parse_file_counts_unparsed(self):
        entries, unparsed = log_parser.parse_file(os.path.join(FIX, "cn_sample.log"), node="cn")
        assert len(entries) == 8
        assert unparsed == 1

    def test_blank_lines_not_counted_unparsed(self, tmp_path):
        p = tmp_path / "with_blanks.log"
        p.write_text(
            "2026-11-20 17:43:54.411 CST [2545,coord(2533,76)] FATAL:  terminating connection due to administrator command\n"
            "\n"
            "not-a-valid-log-line just text\n"
        )
        entries, unparsed = log_parser.parse_file(str(p), node="cn")
        assert len(entries) == 1
        assert unparsed == 1

    def test_missing_file_raises(self):
        with pytest.raises(OSError):
            log_parser.parse_file(os.path.join(FIX, "does_not_exist.log"))


class TestCorrelate:
    def _load(self):
        cn, _ = log_parser.parse_file(os.path.join(FIX, "cn_sample.log"), node="cn")
        dn1, _ = log_parser.parse_file(os.path.join(FIX, "dn1_sample.log"), node="dn1")
        dn2, _ = log_parser.parse_file(os.path.join(FIX, "dn2_sample.log"), node="dn2")
        return cn, dn1 + dn2

    def test_confirmed_dn_failure_via_log_level_cn_lines(self):
        cn, dn = self._load()
        corr = log_parser.correlate(cn, dn)
        by_key = {c["coord"]: c for c in corr}
        # 真实样本：CN 只有 LOG 级连接状态迁移，DN 侧 FATAL —— 应确认为 confirmed
        assert "2533,76" in by_key
        c = by_key["2533,76"]
        assert c["confidence"] == "confirmed"
        assert len(c["dn_entries"]) == 2  # dn1 与 dn2 各一条 FATAL
        assert c["cn_entry"]["level"] == "LOG"

    def test_multiple_cn_identities_paired_to_same_dn_are_candidate(self):
        from datetime import datetime, timedelta, timezone

        from otb_tools.log_parser import LogEntry

        base = datetime(2026, 11, 26, 0, 0, 0, tzinfo=timezone.utc)

        def entry(node, source, seconds, level, pid):
            return LogEntry(
                ts=base + timedelta(seconds=seconds), pid=pid, coord_pid=900, coord_vxid=1,
                level=level, message=f"{node} event", node=node, source=source,
            )

        cn1 = entry("cn0001", "cn1.log", 0, "ERROR", 100)
        cn2 = entry("cn0002", "cn2.log", 40, "LOG", 200)
        dn = entry("dn0001", "dn1.log", 20, "WARNING", 300)
        corr = log_parser.correlate([cn1, cn2], [dn], window=timedelta(seconds=30))[0]
        assert corr["confidence"] == "candidate"
        assert "多个不同/不明 CN 来源" in corr["reason"]

    def test_cn_sources_from_separate_pair_windows_do_not_cause_ambiguity(self):
        from datetime import datetime, timedelta, timezone

        from otb_tools.log_parser import LogEntry

        base = datetime(2026, 11, 26, 0, 0, 0, tzinfo=timezone.utc)

        def entry(node, source, seconds, level, pid):
            return LogEntry(
                ts=base + timedelta(seconds=seconds), pid=pid, coord_pid=900, coord_vxid=1,
                level=level, message=f"{node} event", node=node, source=source,
            )

        cn1 = entry("cn0001", "cn1.log", 0, "ERROR", 100)
        cn2 = entry("cn0002", "cn2.log", 600, "LOG", 200)
        dn1 = entry("dn0001", "dn1.log", 0, "WARNING", 300)
        dn2 = entry("dn0002", "dn2.log", 600, "WARNING", 400)
        corr = log_parser.correlate([cn1, cn2], [dn1, dn2], window=timedelta(seconds=30))[0]
        assert corr["confidence"] == "confirmed"
        assert corr["cn_entry"]["node"] == "cn0002"
        assert [d["node"] for d in corr["dn_entries"]] == ["dn0002"]

    def test_same_cn_identity_across_rotated_sources_remains_confirmed(self):
        from datetime import datetime, timedelta, timezone

        from otb_tools.log_parser import LogEntry

        base = datetime(2026, 11, 26, 0, 0, 0, tzinfo=timezone.utc)

        def entry(source, seconds, level, pid):
            return LogEntry(
                ts=base + timedelta(seconds=seconds), pid=pid, coord_pid=901, coord_vxid=2,
                level=level, message="same coordinator", node="cn0001", source=source,
            )

        cn_a = entry("cn.log", 0, "ERROR", 101)
        cn_b = entry("cn.log.1", 1, "LOG", 102)
        dn = LogEntry(
            ts=base + timedelta(seconds=2), pid=301, coord_pid=901, coord_vxid=2,
            level="WARNING", message="dn event", node="dn0001", source="dn.log",
        )
        corr = log_parser.correlate([cn_a, cn_b], [dn])[0]
        assert corr["confidence"] == "confirmed"

    def test_insufficient_when_dn_side_missing(self):
        cn, _ = log_parser.parse_file(os.path.join(FIX, "cn_sample.log"), node="cn")
        corr = log_parser.correlate(cn, [])
        confs = {c["confidence"] for c in corr}
        assert confs == {"insufficient"}
        # CN 侧 ERROR/FATAL 且 coord>0 的都应报 insufficient
        assert any(c["coord"] == "2334,11" for c in corr)

    def test_time_window_blocks_pid_reuse(self):
        cn, dn = self._load()
        corr = log_parser.correlate(cn, dn, window=timedelta(seconds=1))
        by_key = {c["coord"]: c for c in corr}
        # 样本中 CN 与 DN 的 2533,76 事件时间差 <1s，仍应确认
        assert by_key["2533,76"]["confidence"] == "confirmed"
        # 构造跨天同键：CN 与 DN 同名键但时间差 2 天 → candidate（PID 复用）
        from otb_tools.log_parser import LogEntry
        from datetime import datetime, timezone

        cn_fake = LogEntry(
            ts=datetime(2026, 11, 20, 17, 43, 54, tzinfo=timezone.utc), pid=9001, coord_pid=9001, coord_vxid=9,
            level="ERROR", message="x", node="cn",
        )
        dn_fake = LogEntry(
            ts=datetime(2026, 11, 22, 17, 43, 54, tzinfo=timezone.utc), pid=8001, coord_pid=9001, coord_vxid=9,
            level="FATAL", message="y", node="dn1",
        )
        corr2 = log_parser.correlate([cn_fake], [dn_fake], window=timedelta(seconds=30))
        assert corr2[0]["confidence"] == "candidate"
        assert "PID 复用" in corr2[0]["reason"]

    def test_dn_only_key_reports_insufficient(self):
        cn = []
        dn, _ = log_parser.parse_file(os.path.join(FIX, "dn1_sample.log"), node="dn1")
        corr = log_parser.correlate(cn, dn)
        assert all(c["confidence"] == "insufficient" for c in corr)
        assert any("CN 侧日志无同 coord 记录" in c["reason"] for c in corr)

    def test_candidate_when_both_log_only(self):
        from datetime import datetime, timezone

        from otb_tools.log_parser import LogEntry

        ts = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)
        cn_e = LogEntry(ts=ts, pid=11, coord_pid=11, coord_vxid=1, level="LOG", message="a", node="cn")
        dn_e = LogEntry(ts=ts, pid=22, coord_pid=11, coord_vxid=1, level="LOG", message="b", node="dn1")
        corr = log_parser.correlate([cn_e], [dn_e])
        assert corr[0]["confidence"] == "candidate"

    def test_corr_in_window_dn_only_conservative(self):
        from datetime import datetime, timedelta, timezone

        c = {"coord": "1,1", "cn_entry": {}, "dn_entries": [], "confidence": "insufficient"}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        # CN 侧无记录时无法判断时间，保守视为在窗口内（宁多勿漏，不因缺失崩溃）
        assert log_parser.corr_in_window(c, cutoff) is True
