# 场地电能结算契约（venue-energy audit）

独立于场地占用（venue-occupancy）管道：只用电表实测读数结算，**不允许**从预订量、
容量或任何非读数数据推算用电。Python 3.11+、仅标准库。入口：

```sh
venue-energy audit --meters meters.json --readings readings.csv \
                   --tariffs tariffs.json --output report.json \
                   [--workdir DIR] [--buffer-bytes N] [--buffer-records N] [--fan-in N]
```

成功退出 0 并原子替换输出；任何输入或计算错误退出 2，旧输出原样保留。

## 输入

### meters.json

严格为 `{"meters":[...]}`，不允许其他键、不允许重复 JSON 键。每项恰为
`{meter_id, venue, modulus_wh, max_watts}`：

- `meter_id`、`venue`：非空、去空白、长度 ≤128 的字符串，不含控制字符；`meter_id` 全局唯一。
- `modulus_wh`：1..2^32-1 整数。寄存器合法读数范围为 `[0, modulus_wh)`。
- `max_watts`：0..2^32-1 整数，电表的功率上限（可为 0，表示任何正能耗都不可解释）。

### readings.csv

UTF-8、无 BOM，表头固定且必须逐字为
`meter_id,read_id,observed_at,register_wh`；每行恰好 4 个字段，文件行序任意。

- `observed_at`：2000-01-01T00:00:00Z（含）到 2100-01-01T00:00:00Z（不含）之间的
  UTC epoch 秒，十进制非负整数。
- `register_wh`：十进制非负整数，且必须满足 `0 <= register_wh < modulus_wh`。
- 同一 `read_id` 的解析后字段（meter_id、observed_at、register_wh）完全相同视为
  **重复读数，忽略**；任一字段不同即冲突，**整次运行拒绝**（退出 2）。
- 同一电表同一 `observed_at` 出现不同 `register_wh` 同样整次拒绝；完全相同则去重。
- 引用未知 `meter_id`、非法整数、错误表头、空文件均拒绝。

### tariffs.json

严格为 `{"tariffs":[...]}`。每项恰为
`{venue, start, end, cost_micro_per_kwh, carbon_mg_per_kwh}`：

- 时间为 UTC epoch 秒，区间 `[start,end)` 半开且 `end>start`。
- 两个费率均为非负整数（成本微单位/kWh、碳排 mg/kWh）。
- 同一场地的费率区间半开、互不重叠；首尾相接（前一段 `end` == 后一段 `start`）合法。
- 必须**完整覆盖该场地每一个被判定区间（含不确定区间）的 `[t0,t1)`**；
  出现缺口即拒绝。场地没有任何费率也拒绝。费率可引用的 venue 必须出现在 meters 中。

## 区间解释（决定账单完整性的核心规则）

按 `(meter_id, observed_at)` 排序后，只对同一电表的相邻读数形成半开区间 `[t0,t1)`。
令 `base = R1 - R0`，枚举非负整数回绕次数 k：

```
E(k) = base + k * modulus_wh
可行  <=>  E(k) >= 0  且  3600 * E(k) <= max_watts * (t1 - t0)
```

- 可行 k **恰有一个**时接纳该区间，电量取唯一的 E(k) Wh。
- 可行 k 为 0 个或 ≥2 个时，区间标记为**不确定**：其电量、成本、碳排一律不结算，
  绝不默认取零回绕或最少回绕。
- 不确定区间覆盖到的每一个 UTC 自然月都必须标 `complete=false`，即使该月另有
  有效读数；其后的相邻读数仍各自独立判定，不牵连、不跳过。

## 切片与整数 Wh 分配

每个有效区间先按 **UTC 月界**与**费率边界**切成若干半开子片，再把整数 E Wh 分配到各片：

1. 每片先取 `floor(E * 片时长 / 区间总时长)` Wh；
2. 未分完的余数 Wh 按**时间先后逐 Wh** 分配给最早的子片（循环），各片之和恒等于 E；
3. 因此跨月、跨费率时各片都是整数 Wh 且总量守恒。

成本与碳排不按片四舍五入：每个场地/月把所有片的精确乘积
`wh * 费率 * 时长 / 3_600_000` 以分数累加，月末再对总额做半入四舍五入到整数
（分母固定 3,600,000 = 3600 秒 × 每 kWh 的 1000 Wh）。

## 输出 report.json

```json
{
  "schema_version": 1,
  "input_sha256": {"meters.json": "...", "readings.csv": "...", "tariffs.json": "..."},
  "reading_rows": 9,
  "months": [
    {"venue": "hall", "year": 2000, "month": "01",
     "wh": 3100, "cost_micro": 372000, "carbon_mg": 1395000,
     "complete": false}
  ]
}
```

- `months` 按 `(venue, year, month)` 稳定排序；`month` 为两位字符串。
- 每个出现过判定区间（有效或不确定）的场地/月恰有一行；只有不确定区间、没有任何
  接纳电量的月份也会出现，`wh/cost_micro/carbon_mg` 为 0 且 `complete=false`。
- `reading_rows` 是去重前实际解析的数据行数（含被忽略的重复行）。
- 输出为确定性结果：同输入逐字节可复现；原子发布（同目录临时文件 + `os.replace`），
  输出路径不得等于任一输入，符号链接输出被拒绝。

## 资源边界

输入最多 500 万行。除 meters/tariffs 等有界小文档外，内存不随行数增长：
读数经定长二进制 run 文件做两路外部排序（read_id 序去重、meter 时序扫描），
run 以 1 MiB 定长块流式读取，合并路数经 fan-in 分层封顶。峰值 RSS 实测在
500 万行下约 33 MiB（上限 128 MiB），25 万→500 万行峰值基本持平。临时结构只落在
`--workdir`（默认系统临时目录），正常结束与异常路径都清理 `.run` 文件。
