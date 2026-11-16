# 自制场地电能样例（无外部数据）

三个文件均为本仓手工构造的最小演示数据，不来自任何真实系统或第三方。

- `meters.json`：hall 两块表（含一次寄存器回绕）、annex 一块表。
- `readings.csv`：行序故意打乱，含一个完全重复 read_id、一个被唯一解释的
  solar 区间和一个多回绕可行的不确定区间；annex 区间跨越费率边界。
- `tariffs.json`：hall 在 2000-01-15 换价，区间半开相接、完整覆盖 2000–2100。

运行：
`PYTHONPATH=src python3 -m venue_energy.cli audit --meters fixtures/energy/meters.json
--readings fixtures/energy/readings.csv --tariffs fixtures/energy/tariffs.json
--output run/energy-report.json`
