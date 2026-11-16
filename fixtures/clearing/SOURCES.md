# 自制支付与银行到账核销样例（无外部数据）

本目录五个文件均为本仓手工构造的最小演示数据，不来自任何真实系统或第三方。

- `payments.json`：hall/USD/12000 的 P-100（别名 REF-A、REF-B）、P-200（REF-B），
  以及 hall/EUR/12000 的 P-300（REF-C）。
- `credits.json`：C-1(REF-A)、C-2(REF-B、REF-A)、C-3(REF-C) 均为 hall/USD/12000；
  C-4 为 annex/EUR/12000，C-5 为 hall/USD/10000，C-9 为 annex/USD/999。
  候选边恰为 P-100–C-1、P-100–C-2、P-200–C-2；多项别名/附言交集只算一条边，
  同额但币种或场地不同者不成边。
- `pin.json`：绑定 P-100 与 C-1。
- `unpin.json`：解绑 P-100。

两个导入文件都含两个键（`payments`/`credits`），因此 `import` 严格要求的两键形状
对任一文件都成立；样例只演示支付先到、到账后到，以及反向顺序同样可工作
（反向顺序由 `tests/vc_test_source.py` 验证）。

运行：

```sh
PYTHONPATH=src python3 -m venue_clearing.cli init   --db run/clearing.db
PYTHONPATH=src python3 -m venue_clearing.cli import --db run/clearing.db --source fixtures/clearing/payments.json
PYTHONPATH=src python3 -m venue_clearing.cli import --db run/clearing.db --source fixtures/clearing/credits.json
PYTHONPATH=src python3 -m venue_clearing.cli report --db run/clearing.db --output run/clearing-report.json
PYTHONPATH=src python3 -m venue_clearing.cli decide --db run/clearing.db --source fixtures/clearing/pin.json
PYTHONPATH=src python3 -m venue_clearing.cli report --db run/clearing.db --output run/clearing-pinned.json
PYTHONPATH=src python3 -m venue_clearing.cli decide --db run/clearing.db --source fixtures/clearing/unpin.json
PYTHONPATH=src python3 -m venue_clearing.cli report --db run/clearing.db --output run/clearing-unpinned.json
```

预期报告逐字段见 [clearing-contract.md](../../docs/clearing-contract.md) 的样例一节，
`tests/vc_test_cli.py` 逐字段核对。
