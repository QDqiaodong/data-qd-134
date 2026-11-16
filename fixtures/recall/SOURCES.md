# 自制原料批次谱系与召回样例（无外部数据）

本目录三个文件均为本仓手工构造的最小演示数据，不来自任何真实系统或第三方。

- `init.json`：登记 hall、annex 两个场地。
- `operations.json`：两个原料批次（flour-a 1000 g、flour-b 600 g），一次 400+200→600
  混匀成 dough-x，向 hall 送达 150 g，再把 dough-x 300 g 与 flour-a 100 g 混成
  dough-y 400 g，向 annex 送达 80 g。
- `recall.json`：召回原始批次 flour-b。

运行：

```sh
PYTHONPATH=src python3 -m venue_recall.cli init   --db run/recall.db --source fixtures/recall/init.json
PYTHONPATH=src python3 -m venue_recall.cli ingest --db run/recall.db --source fixtures/recall/operations.json
PYTHONPATH=src python3 -m venue_recall.cli recall --db run/recall.db --source fixtures/recall/recall.json
PYTHONPATH=src python3 -m venue_recall.cli report --db run/recall.db --output run/recall-report.json
```

预期报告（flour-b 质量守恒：库存 50+80+400 加送达 50+20 恰为收货 600 g）见
[recall-contract.md](../../docs/recall-contract.md) 的样例一节。
