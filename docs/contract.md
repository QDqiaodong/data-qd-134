# Booking capacity audit v2

v2 = 原 v1 静态报告 + 本地修订数据集。v1 的全部语义保持不变：面向场地运营，审计预订占用而非传感器实测；
输入显式 horizon(start,end)，1..100 个 venues(id,capacity)，最多 20000 条 bookings(id,venue,start,end,quantity)。
时间为 0..10^12 整数 tick，horizon 和 booking 必须正时长；容量 0..10^6 整数，quantity 为 1..10^6，
id 唯一、venue 引用完整。容量 0 合法表示不容许预订。预订为 [start,end) 半开区间，先裁剪到 horizon，
空交集无贡献；同刻离开和进入不产生瞬间超容。扫描事件边界，输出覆盖完整 horizon 的分段
occupancy/excess，并合并相邻等占用段。peak=max occupancy；over_capacity_ticks 为 excess>0 的时长，
excess_unit_ticks 为超量×时长，booked_unit_ticks 为占用×时长。每个场地包括无预订场地也有完整报告。
样例 hall 峰值4、超容5 tick、超额5 unit-tick、总预订55 unit-tick；quiet 全程0。
无子命令的 `--source/--output` 调用与 v1 逐字节兼容。

## 数据集命令

`venue-occupancy init|apply|export`，状态完全位于一个 SQLite 文件中，仅用标准库，Python >=3.11。

- `init --db D --source JSON`：接收 v1 契约 JSON，冻结 horizon 与 venues，初始 bookings 以 rev=0 写入，
  `generation` 置 0。已初始化的文件拒绝再次 init（退出2）。
- `apply --db D --source JSON`：JSON 严格为 `{"revisions":[...]}`，最多 20000 项、输入最多 8MB
  （沿用原 JSON 读取：拒绝重复键与非有限数）。
- `export --db D --output JSON`：在一个 SQLite 读快照内输出 `{"generation":N,"result":{...}}`，
  `result` 沿用 v1 格式。安全发布：临时文件、原子替换、拒绝输出符号链接；任何失败退出2并保留旧文件。

## 修订契约

记录形状严格：upsert 恰为 `{id,rev,op,venue,start,end,quantity}`，delete 恰为 `{id,rev,op}`，
`op` 只能是 `upsert`/`delete`；其他字段沿用 v1 校验（标识符、整数范围、venue 必须冻结已知，
upsert 必须正时长）。delete 指向未知 id 合法。`rev` 为 1..10^12 整数。

### 有效版本

每个 id 的最大 rev 是唯一有效版本。最高版本为 delete 则该 id 不贡献占用；低版本仍完整留存于事实表，
但既不能复活也不能覆盖高版本。乱序递交与批内排列都不改变最终有效集合。

### 重放与冲突

同一 `(id,rev)` 解析后的完整字段相同即重放（不新增事实、不增加 generation）；不同即冲突，
整批拒绝并保持数据库不变——即使该 rev 已被更高版本取代。比较在原始字段上进行，
因此 `{id:"x",rev:1,op:"delete"}` 与任何 upsert 都不同，delete 的额外字段或 upsert 的缺失字段
在解析阶段就因字段集不符而失败。

### generation

新增事实使 generation 加一；完全重放不增加；批次中没有任何新事实时不增加。初始为0。
事实、有效指针、受影响分段、全部派生指标与 generation 在同一个事务内可见。

## 物化契约

`segments` 为覆盖完整 horizon 的半开分段，行序固定为 (venue,start)，相邻等占用段唯一合并，
空场保留 `{start:horizon.start,end:horizon.end,occupancy:0,excess:0}`。
四项指标 `peak/over_capacity_ticks/excess_unit_ticks/booked_unit_ticks` 与原静态模型对当前有效集合
逐项相等；导出行的顺序、分批方式与重放次数都不改变结果。导出内容即唯一事实源：
它等价于把当前有效集合重新交给 `model.analyze`。

## 持久表与索引

| 表 | 主键 / 索引 | 内容 | 增长 |
| --- | --- | --- | --- |
| `meta` | PK(key) | schema 版本、冻结 horizon、generation | 常数 |
| `venues` | PK(id) | 冻结场地容量 | ≤100 行 |
| `facts` | PK(id,rev) WITHOUT ROWID | 全部修订（含被取代） | 随历史 |
| `effective` | PK(id)；索引(venue,op) | 每个 id 的有效版本或 delete 指针 | ≤有效 id 数 |
| `segments` | PK(venue,start) WITHOUT ROWID | 完整 horizon 的最大等占用段 | ≤有效区间端点数 |
| `venue_levels` | PK(venue,occupancy) WITHOUT ROWID | 占用值→段数直方图，零计数行即删 | ≤场地内不同占用值 |
| `venue_metrics` | PK(venue) WITHOUT ROWID | peak 与三项汇总（大整数以文本存） | 每场地一行 |

`effective(venue,op)` 只服务 init 时的逐场地一次重建与外部审计，apply 从不扫描它。
所有 apply 查询都是主键等值/范围 seek：`facts` 按 `(id,rev)`、`effective` 按 id、
`segments` 按 `(venue,start)`、`venue_levels` 按 `(venue,occupancy)`、`venue_metrics` 按 venue。

## 局部维护策略与正确性

apply 先对每个受影响 id 求出「旧有效版本 → 新有效版本」的差集，再按场地把旧区间记为负贡献、
新区间记为正贡献，裁剪到 horizon 后按端点聚成簇。每簇只重写包含该簇的既有段（起点所在段到终点所在段），
随后在两个区域边界各做一次相邻同占用合并。要点：

- 簇之间的空档从不进入任何重写区域；跨场地改期在旧场地消除旧贡献、在新场地加入新贡献，
  相距很远的两个区间不会把中间空档扩进重算区。
- 区域内用旧段占用叠加修订增量，精确处理区域内其他预订造成的占用变化；
  区域边界通过一次等值检查合并，保证相邻等占用段唯一。
- 指标按「新区域 − 旧区域」增量修补；peak 由 `venue_levels` 的一次降序索引 seek 得出，
  不从段或时间轴推导。
- 事实版本判定先于物化：过期或重放记录（拖高不了有效版本）不产生任何物化写入，
  因此即使其载荷不同也不能触碰占用；冲突在写入前整批拒绝。

## 资源代价

有效预订可达 20 万、保留修订 200 万。单批只改少数 id 时，apply 的工作量是本批 id 数、
其旧新区间并集覆盖的段数与场地直方图差值的函数，与库中事实总数、有效预订总数、horizon 长度无关；
不按 tick 展开。export 顺序遍历场地与分段，内存不随库总行数增长（逐场地流式写出）。
init 需要 O(有效预订) 内存与一次全量排序。

## 事务、并发与恢复

apply 在一个 `BEGIN IMMEDIATE` 事务里写事实、有效指针、分段、直方图、指标与 generation，
崩溃或注入异常后整体回滚，重启后与无故障结果一致。init 同样是单事务。
数据库使用 WAL，export 持有稳定读快照，可与一个写事务并发；观察者只能取得完整旧代或完整新代。
单一写入者，不承诺多写者并发。

## 明确的非目标

不做真实预约、取消审批、场地分配或按人去重；不扩展 horizon；容量与场地在 init 后不可变；
不提供保留策略或修订压缩（事实只增）；不依赖外部服务或网络。
