# 局部维护的可复核证据

本文记录验收要求的查询轨迹与计数，全部可由仓内测试独立复跑，不依赖任何外部服务或 profiler。

## 1. 同一局部更正，小库 vs 大库

- 小库：2 个场地、3 条预订、1 条待更正 id。
- 大库：额外 40 个场地、4000 个无关 id、2000 个额外分段密集的远区间；
  两者在待更正 id 周围的分段结构与右边界完全一致，只有无关数据量不同。
- 更正：把 `base0` 从 `[0,1000) q=1` 改为 `[500,600) q=3`，horizon 为 `[0,10^9)`。

`apply` 的存储层计数（`tests/test_locality.py::test_row_counts_do_not_grow_with_unrelated_data`）：

| 计数 | 小库 | 大库 |
| --- | --- | --- |
| `facts_read` | 1 | 1 |
| `effective_read` | 1 | 1 |
| `segments_read` | 6 | 6 |
| `segments_written` | 5 | 5 |
| `levels_written` | 3 | 3 |
| `metrics_read/written` | 1 / 1 | 1 / 1 |

SQL 语句序列在两种规模下逐条相同（仅 `venue_metrics` 的数值不同）。`apply` 的 SQL 轨迹中
不存在 `FROM facts` / `FROM effective` / `FROM segments` / `FROM venue_levels` 的无条件扫描，
不存在 `COUNT(`/`SUM(`，也不存在聚合扫描；所有 `segments` 读取都是
`WHERE venue=? AND start ...` 的主键 seek（`test_apply_never_scans_large_tables`）。

第三重测量用未修改的 SQLite：整个 `apply` 包在每 1000 条 VM 指令触发一次的 progress
handler 里，两种规模相差不超过 10 次回调（`test_vm_steps_do_not_scale_with_database`）。

## 2. 跨场地改期与远距区间

- 把 `base1` 从 `other` 的 `[0,1000)` 迁到 `target` 的 `[900000,900100)`：
  旧场地消除旧贡献、新场地加入新贡献，两个区间的中间空档（0..900000）不进入任何重写区域。
- 当同一 id 的旧区间与新区间相距很远（同一场地内从 0 迁到 900000），它们形成两个独立簇：
  分别只重写各自的包含段，随后各做一次边界合并探测。中间空档不被读取或改写。

## 3. 事务故障与重开

`tests/test_transactions.py`：

- 注入「事实已写、分段已写、尚未提交」的异常后，`facts`、`effective`、`segments`、
  `venue_levels`、`venue_metrics`、`meta` 的逐行快照与故障前完全相同，`generation` 仍为旧值；
  重试一次后的库与无故障干净的库逐行相等，导出逐字节相等。
- 一个进程持续导出、另一个进程连续提交时，采集到的每次导出都恰好等于某一代的完整结果
  （不会出现半新半旧）。

## 4. 与静态模型的独立全量对照

`tests/test_materialize.py` 在小规模上做全量对照：

- 嵌套、首尾相接、零容量、裁剪为空、撤回最后一条预订、跨场地迁移、边界合并、
  过期版本不重绘等定向场景，`segments` 与四项指标逐项等于 `model.analyze`。
- 400 步随机操作序列逐步对照；另外把 200 条随机修订按「一次提交」与「每 7 条一批」
  两种方式投递，再重放前 50 条，最终结果相同。
- 期望值不是用 apply 的代码路径推导的：它从 `facts` 表独立扫描、在 Python 里取每 id 最大 rev，
  再交给原 `model.analyze`。

## 5. 规模

在 20000 条初始预订之上补齐到 20 万有效 id、202 万条修订（1 个 id 10 个版本）后：

- 单条局部更正的 apply 耗时 3–6 ms，与库中修订总数无关；
- 全量 export 约 0.4 s，输出约 26 MiB，逐场地流式写出。

## 6. 如何复跑

```sh
PYTHONPATH= python3 -m unittest discover -s tests -t . -v
```

或安装后 `.venv/bin/python -m unittest discover -s tests -t . -v`。
测试使用临时目录，不写仓库，不需要网络。
