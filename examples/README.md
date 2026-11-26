# 离线诊断样例

三个 `.txt` 文件保存为 JSON 格式，均由当前 CLI 于 2026-11-26 生成。SQLSTATE 和错误文本示例只使用命令行输入，不连接数据库；CN/DN 示例读取 `tests/fixtures/logs/` 中固定的 2026-11-20 日志，不连接集群，也不代表当前实机状态。

以下命令从仓库根目录运行。日志样例使用 168 小时窗口以覆盖固定日志时间；之后重跑时，需按运行日期增加窗口，确保覆盖 2026-11-20 的事件。CN 样例含一行无法解析的文本，输出会在 `unparsed_lines` 中计数。

## SQLSTATE 42704

```bash
PYTHONPATH=src python3 -m otb_tools.cli diagnose \
  --sqlstate 42704 --format json \
  --output examples/diagnose-42704.txt
```

预期：`name` 为 `undefined_object`，`confidence` 为 `confirmed`，退出码 0。

## Connection refused

```bash
PYTHONPATH=src python3 -m otb_tools.cli diagnose \
  --text 'psql: could not connect to server: Connection refused' \
  --format json \
  --output examples/diagnose-connection-refused.txt
```

预期：命中 `connection-refused`，`confidence` 为 `confirmed`，退出码 0。

## CN/DN 固定日志关联

```bash
PYTHONPATH=src python3 -m otb_tools.cli diagnose \
  --cn-logs tests/fixtures/logs/cn_sample.log \
  --dn-logs tests/fixtures/logs/dn1_sample.log,tests/fixtures/logs/dn2_sample.log \
  --window-hours 168 --format json \
  --output examples/diagnose-cn-dn-correlate.txt
```

预期：`error_fatal_in_window` 为 8；`correlations` 中 `coord=2533,76` 为 `confirmed`；`root_cause.determined` 为 `false`；退出码 0。
