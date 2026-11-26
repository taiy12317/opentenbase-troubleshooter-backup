import re

from otb_tools import cli, inspector
from otb_tools.models import Report, Status

NODES = [
    {"node_name": "cn0001", "node_type": "C", "node_id": 0},
    {"node_name": "dn0001", "node_type": "D", "node_id": 1},
    {"node_name": "dn0002", "node_type": "D", "node_id": 2},
]

CAT_COLS = ["oid", "nspname", "relname", "reltuples", "pclocatortype", "discolnums", "nodeoids"]


def _make_run_sql(catalog_rows, counts=None, stats_rows=None, count_errors=None,
                  attname="id"):
    counts = counts or {}
    stats_rows = stats_rows if stats_rows is not None else []
    count_errors = count_errors or {}

    def fake(conn, sql, params=None):
        if "FROM pg_class" in sql:
            return catalog_rows, CAT_COLS
        m = re.search(r'FROM "([^"]+)"\."([^"]+)"', sql)
        if m:
            key = f"{m.group(1)}.{m.group(2)}"
            if key in count_errors:
                raise RuntimeError(count_errors[key])
            return counts.get(key, []), ["xc_node_id", "count"]
        if "FROM pg_attribute" in sql:
            return [(attname,)], ["attname"]
        if "FROM pg_stats" in sql:
            return stats_rows, ["attname", "n_distinct", "most_common_freqs"]
        raise AssertionError(f"unexpected sql: {sql}")

    return fake


def _run_skew(monkeypatch, catalog_rows, cfg=None, nodes=None, **kw):
    monkeypatch.setattr(inspector, "run_sql", _make_run_sql(catalog_rows, **kw))
    return inspector._skew_item(object(), nodes or NODES, cfg or inspector.InspectorConfig())


class TestSkewUnit:
    def test_replicated_and_no_locator_skipped(self, monkeypatch):
        catalog = [
            (1, "public", "t_rep", 100, "R", "1", ""),
            (2, "public", "t_local", 50, None, None, ""),
        ]
        item = _run_skew(monkeypatch, catalog)
        assert item.status == Status.SKIP
        assert "复制分布" in item.evidence[0]
        assert "无 pgxc_class" in item.evidence[1]
        assert "未精确统计任何分片表" in item.summary
        assert any("不参与倾斜判定" in f for f in item.facts)

    def test_balanced_pass(self, monkeypatch):
        catalog = [(1, "public", "t_bal", 1000, "H", "1", "")]
        item = _run_skew(monkeypatch, catalog, counts={"public.t_bal": [(1, 512), (2, 488)]})
        assert item.status == Status.PASS
        assert any("dn0001=512 dn0002=488" in e and "均衡" in e for e in item.evidence)

    def test_ratio_skew_warn(self, monkeypatch):
        catalog = [(1, "public", "t_skew", 1000, "H", "1", "")]
        item = _run_skew(monkeypatch, catalog, counts={"public.t_skew": [(1, 800), (2, 200)]})
        assert item.status == Status.WARN
        assert "行数不均衡" in item.summary
        assert any("4.00" in e and "不均衡" in e for e in item.evidence)
        assert any("不影响查询正确性" in c for c in item.causes)

    def test_complete_skew_fail(self, monkeypatch):
        catalog = [(1, "public", "t_all", 20000, "H", "1", "")]
        item = _run_skew(monkeypatch, catalog, counts={"public.t_all": [(1, 15000)]})
        assert item.status == Status.FAIL
        assert "完全倾斜" in item.summary
        assert any("dn0002=0 行" in e for e in item.evidence)
        assert "重建表" in item.fix_hint

    def test_complete_skew_small_downgraded_warn(self, monkeypatch):
        catalog = [(1, "public", "t_all", 5000, "H", "1", "")]
        item = _run_skew(monkeypatch, catalog, counts={"public.t_all": [(1, 5000)]})
        assert item.status == Status.WARN
        assert any("降级 WARN" in e for e in item.evidence)

    def test_big_table_skipped_with_stats_hint(self, monkeypatch):
        catalog = [(1, "public", "t_big", 200_000, "H", "2", "")]
        item = _run_skew(
            monkeypatch, catalog,
            stats_rows=[("id", "-0.05", "{0.9,0.05}")],
        )
        assert item.status == Status.SKIP  # 全部表超阈值未扫描：未做判定
        assert "未精确统计任何分片表" in item.summary
        assert any("超过 --skew-max-rows" in e for e in item.evidence)
        hint = next(e for e in item.evidence if "零扫描倾斜风险提示" in e)
        assert "比例型" in hint and "最常见值占 90%" in hint

    def test_n_distinct_minus_one_no_hint(self, monkeypatch):
        # PG 语义：n_distinct=-1 表示不同值数与行数相等（全不同），不是倾斜风险
        catalog = [(1, "public", "t_big", 200_000, "H", "2", "")]
        item = _run_skew(
            monkeypatch, catalog,
            stats_rows=[("id", "-1", "")],
        )
        assert item.status == Status.SKIP
        assert not any("零扫描倾斜风险提示" in e for e in item.evidence)

    def test_n_distinct_fraction_hint(self, monkeypatch):
        # -0.5 = 不同值占 50%，占比不低，不提示；仅小占比（<-0.1）提示
        catalog = [(1, "public", "t_big", 200_000, "H", "2", "")]
        item = _run_skew(
            monkeypatch, catalog,
            stats_rows=[("id", "-0.5", "")],
        )
        assert not any("零扫描倾斜风险提示" in e for e in item.evidence)

    def test_unanalyzed_skipped(self, monkeypatch):
        catalog = [(1, "public", "t_new", -1, "H", "1", "")]
        item = _run_skew(monkeypatch, catalog)
        assert item.status == Status.SKIP  # 无统计信息且未显式指定：未做判定
        assert "未精确统计任何分片表" in item.summary
        assert any("未收集统计信息" in e for e in item.evidence)

    def test_explicit_skew_tables_forced(self, monkeypatch):
        catalog = [(1, "public", "t_big", 200_000, "H", "1", "")]
        cfg = inspector.InspectorConfig(skew_tables=["public.t_big"])
        item = _run_skew(monkeypatch, catalog, cfg=cfg,
                         counts={"public.t_big": [(1, 100), (2, 100)]})
        assert item.status == Status.PASS
        assert any("dn0001=100 dn0002=100" in e for e in item.evidence)

    def test_bare_table_name_match(self, monkeypatch):
        catalog = [(1, "public", "t_big", 200_000, "H", "1", "")]
        cfg = inspector.InspectorConfig(skew_tables=["t_big"])
        item = _run_skew(monkeypatch, catalog, cfg=cfg,
                         counts={"public.t_big": [(1, 900), (2, 100)]})
        assert item.status == Status.WARN

    def test_catalog_error_is_error(self, monkeypatch):
        def boom(conn, sql, params=None):
            raise RuntimeError("catalog boom")

        monkeypatch.setattr(inspector, "run_sql", boom)
        item = inspector._skew_item(object(), NODES, inspector.InspectorConfig())
        assert item.status == Status.ERROR
        assert "分布元数据读取失败" in item.summary

    def test_count_error_is_error(self, monkeypatch):
        catalog = [(1, "public", "t", 100, "H", "1", "")]
        item = _run_skew(monkeypatch, catalog, count_errors={"public.t": "query timeout"})
        assert item.status == Status.ERROR
        assert "证据不完整" in item.summary
        assert any("行数统计失败" in e for e in item.evidence)

    def test_no_user_tables_skip(self, monkeypatch):
        item = _run_skew(monkeypatch, [])
        assert item.status == Status.SKIP

    def test_single_dn_skip(self, monkeypatch):
        nodes = NODES[:2]
        monkeypatch.setattr(inspector, "run_sql", _make_run_sql([(1, "public", "t", 1, "H", "1", "")]))
        item = inspector._skew_item(object(), nodes, inspector.InspectorConfig())
        assert item.status == Status.SKIP
        assert "不存在跨节点倾斜概念" in item.summary


class TestSkewNodeoids:
    """pgxc_class.nodeoids 表级分布节点范围：期望节点集以表级元数据为准。"""

    NODES3 = [
        {"node_name": "cn0001", "node_type": "C", "node_id": 0, "oid": 100},
        {"node_name": "dn0001", "node_type": "D", "node_id": 1, "oid": 101},
        {"node_name": "dn0002", "node_type": "D", "node_id": 2, "oid": 102},
        {"node_name": "dn0003", "node_type": "D", "node_id": 3, "oid": 103},
    ]

    def _run3(self, monkeypatch, catalog, **kw):
        return _run_skew(monkeypatch, catalog, nodes=self.NODES3, **kw)

    def test_table_level_range_balanced_pass(self, monkeypatch):
        # 3 DN 集群，但表只分布在 dn0001/dn0002：按 nodeoids 判定，不出现虚假的 0 行节点
        catalog = [(1, "public", "t", 100, "H", "1", "101 102")]
        item = self._run3(monkeypatch, catalog, counts={"public.t": [(1, 512), (2, 488)]})
        assert item.status == Status.PASS
        assert any("dn0001=512 dn0002=488" in e and "均衡" in e for e in item.evidence)
        assert not any("0 行" in e for e in item.evidence)

    def test_nodeoids_names_zero_row_nodes(self, monkeypatch):
        catalog = [(1, "public", "t", 100, "H", "1", "101 102 103")]
        item = self._run3(monkeypatch, catalog, counts={"public.t": [(1, 100)]})
        assert item.status == Status.WARN  # 100 行低于 FAIL 阈值，降级
        assert any("dn0002、dn0003=0 行" in e for e in item.evidence)

    def test_unmatched_nodeoids_oid_fact(self, monkeypatch):
        # nodeoids 无法完全映射到 pgxc_node：节点范围不可靠，不得基于回退 DN 集断言 0 行节点，也不得判均衡
        catalog = [(1, "public", "t", 100, "H", "1", "101 999")]
        item = self._run3(monkeypatch, catalog, counts={"public.t": [(1, 50), (2, 50)]})
        assert item.status == Status.WARN
        assert any("无法映射到 pgxc_node" in f for f in item.facts)
        assert any("分布元数据异常" in e for e in item.evidence)
        assert not any("0 行" in e for e in item.evidence)
        assert not any("→ 均衡" in e for e in item.evidence)

    def test_unexpected_node_meta_problem(self, monkeypatch):
        # counts 含 nodeoids 范围外的节点（dn0003）：元数据不一致，单独报告，不判均衡
        catalog = [(1, "public", "t", 1000, "H", "1", "101 102")]
        item = self._run3(monkeypatch, catalog, counts={"public.t": [(1, 50), (2, 50), (3, 50)]})
        assert item.status == Status.WARN
        assert any("分布范围外的节点" in f for f in item.facts)
        assert any("分布元数据异常" in e for e in item.evidence)
        assert not any("0 行" in e for e in item.evidence)
        assert not any("→ 均衡" in e for e in item.evidence)

    def test_unknown_xc_node_id_fact(self, monkeypatch):
        catalog = [(1, "public", "t", 1000, "H", "1", "101 102")]
        item = self._run3(monkeypatch, catalog, counts={"public.t": [(1, 80), (2, 100), (9, 20)]})
        assert any("未注册到 pgxc_node" in f for f in item.facts)
        # 未注册节点使计数无法与节点名可靠对应：不判均衡、不断言 0 行节点
        assert item.status == Status.WARN
        assert not any("0 行" in e for e in item.evidence)
        assert not any("→ 均衡" in e for e in item.evidence)


class TestSkewCli:
    def test_skew_options_passthrough(self, monkeypatch):
        captured = {}

        def fake_run(cfg, strict=False):
            captured["cfg"] = cfg
            return Report(command="inspect")

        monkeypatch.setattr(inspector, "run", fake_run)
        rc = cli.main([
            "inspect",
            "--skew-tables", "public.t,other",
            "--skew-max-rows", "5000",
            "--skew-fail-min-rows", "999",
        ])
        assert rc == 0
        cfg = captured["cfg"]
        assert cfg.skew_tables == ["public.t", "other"]
        assert cfg.skew_max_rows == 5000
        assert cfg.skew_fail_min_rows == 999

    def test_skew_defaults(self, monkeypatch):
        captured = {}

        def fake_run(cfg, strict=False):
            captured["cfg"] = cfg
            return Report(command="inspect")

        monkeypatch.setattr(inspector, "run", fake_run)
        cli.main(["inspect"])
        cfg = captured["cfg"]
        assert cfg.skew_tables == []
        assert cfg.skew_max_rows == inspector.SKEW_MAX_ROWS_DEFAULT
        assert cfg.skew_fail_min_rows == inspector.SKEW_FAIL_MIN_ROWS_DEFAULT
