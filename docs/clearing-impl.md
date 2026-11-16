# 支付核销实现说明（venue-clearing）

模块布局（`src/venue_clearing/`，与 `venue_occupancy/`、`venue_energy/`、
`venue_recall/` 完全分离、零互相 import；不修改旧库表，不共用数据库文件）：

| 模块 | 职责 |
| --- | --- |
| `contract.py` | 三个输入文档的严格校验、别名/附言集合的排序规范化、重放规范载荷 |
| `graph.py` | Hopcroft-Karp 最大匹配 + 全解空间分类（单次线性判定，不枚举匹配） |
| `store.py` | SQLite 表结构、WAL 连接、只读快照连接 |
| `dataset.py` | init / import / decide / report 的事务语义、分量维护与推断重算 |
| `common.py` | 受限 JSON（≤8MB、拒重复键/NaN）、原子发布 |
| `cli.py` | `venue-clearing` 入口与退出码 |

## 持久化结构

| 表 | 主键 / 索引 | 内容 | 增长 |
| --- | --- | --- | --- |
| `meta` | PK(key) | schema 版本、generation、maximum_pairs、next_component | 常数 |
| `payments` | PK(payment_id) | 场地/币种/金额 + 规范载荷 | 随来源 |
| `credits` | PK(credit_id) | 同上 | 随来源 |
| `payment_alias` | PK(alias,payment_id) WITHOUT ROWID | 别名→支付倒排 | 随来源 |
| `credit_reference` | PK(reference,credit_id) WITHOUT ROWID | 附言→到账倒排 | 随来源 |
| `edges` | PK(payment_id,credit_id) WITHOUT ROWID；索引(credit_id) | 候选边，一项一对 | 随候选 |
| `components` | PK(component_id) | 每个**原始候选连通分量**一行：顶点数、边数、去绑定后的最大匹配数 | 随分量 |
| `comp_vertex` | PK(kind,vertex_id) WITHOUT ROWID；索引(component_id) | 顶点归属 | 恰一顶点一行 |
| `inference` | PK(kind,vertex_id) WITHOUT ROWID | 每顶点状态、对端列表、可未匹配标志 | 恰一顶点一行 |
| `bindings` | PK(payment_id)，UNIQUE(credit_id) | 当前人工绑定 | 随绑定 |
| `decisions` | PK(decision_id) | 追加式决定日志 + 规范载荷 | 随决定 |
| `imports` | PK(import_id) | 追加式来源日志：每个真正新增事实的批次一行，记录它产生的 generation 与新记录/新边数 | 随写入批次 |

`inference` 的 `peers` 存为紧凑 JSON 数组文本，`report` 直接原样写出，避免重复
序列化整行。

## 三条索引/结构如何共同保证正确性

### 1. 倒排索引决定“候选边”

候选性 = 场地、币种、金额全等 **且** 别名集合与附言集合有交集。导入一批时，对每条
新 payment 执行

```sql
SELECT DISTINCT cr.credit_id FROM credit_reference cr
  JOIN credits c ON c.credit_id=cr.credit_id
 WHERE cr.reference IN (本批别名...) AND c.venue=? AND c.currency=? AND c.amount_minor=?
```

对每条新 credit 做对称查询。这样：

- 新到账能发现**先到**的支付，新支付也能发现**先到**的到账，两种到达顺序结果相同；
- `SELECT DISTINCT` 与主键 `(alias,payment_id)` 使多项交集自然只产生一条边，
  `INSERT OR IGNORE` 保证重复导入不重复建边；
- 每条查询只由本批的别名/附言键驱动，命中行数与原记录规模无关，不会扫描
  `payments × credits`。

既有记录的候选性是它们自身字段的纯函数，因此**已存在记录之间不会因为新批而产生
新边**；新批只需考虑“本批新记录 ↔ 库中/本批另一侧记录”的组合，正是上面的两次
倒排探测。

### 2. 分量表把“重新分类”限制在一个连通分量

`comp_vertex` 恰一顶点一行，`components` 恰一原始分量一行（孤立顶点也是自成一
分量的单行）。import 时用并查集把本批新边连起来的顶点节点、以及每个被触及的旧
分量合并成组；每组只在合并后重新分类一次，并把旧分量的 `comp_vertex` 行改写为
新分量、删除多余 `components` 行。decide 时 pin/unpin 只定位支付所在
`comp_vertex.component_id`，重新分类该分量。

因为匹配问题在不相交连通分量之间可加，**跨分量不会互相影响**；所以：

- 绑定只需重算它所在分量，但必须重算该分量的**全部**顶点（不是两行绑定）；
- 新增边桥接两个旧分量时，合并后的整分量必须重算，旧推断随之失效；
- `maximum_pairs` 在 `meta` 中增量维护：导入时加上新分量/合并分量
  （去绑定后最大匹配数）的差值；决定时加上绑定数变化与该分量最大匹配数变化。

### 3. 全解空间判定结构（不枚举最大匹配）

对一个已去掉绑定端点的二分量图，取任一个最大匹配 M，构造有向图 D：

- **匹配边**从右侧到左侧，**非匹配边**从左侧到右侧；
- 从**未匹配左顶点**可达的顶点记为 X；能到达**未匹配右顶点**的顶点记为 Y；
- 求 D 的强连通分量（SCC）。

判定：一条边 `(u,v)` 出现在某个最大匹配中，当且仅当
`(u,v) ∈ M`，或 `u ∈ X`，或 `v ∈ Y`，或 `u,v` 同属一个 SCC。左顶点可未匹配当且
仅当它在 X 中；右顶点可未匹配当且仅当它在 Y 中。这正是二分图交替路/增广路的
经典刻画：X 中的左顶点可沿交替奇路到达自由右顶点而重新分配，Y 中的右顶点可沿
交替偶路把空位推给自由右顶点，同一 SCC 内部可沿交替环互换匹配。

实现只做一次 Hopcroft-Karp（O(E√V)）、两次 BFS（X/Y）与一次迭代式 Kosaraju，
再扫一遍边判定归属：**没有枚举任何最大匹配**，也没有对每条候选边重跑一次最大
匹配。一个由 k 个 4-环组成的 4000 顶点分量有 2^2000 个最大匹配，仍是一次线性
扫描（`tests/vc_test_classify.py` 用 2^400 的构造与运行时间上界证明）。

该判定在测试中与独立穷举 oracle 对拍：3×3 以内**穷举所有图**，7×7 以内随机
数万图，覆盖交替环、缺额链、孤立点以及“局部有多个候选但全局被迫”的配对；
持久化层面再按随机导入/绑定序列逐状态与 oracle 全量报告对拍
（`tests/vc_test_linkage.py`）。

### 绑定两端的状态

绑定顶点不进入匹配图，两者状态写作 `pinned`、`possible_peers` 为其唯一对端、
`can_be_unmatched=false`。剩余顶点按上述判定分类。`maximum_pairs` 因此等于
`绑定数 + Σ 分量去绑定后最大匹配数`，可以由 report 的 `pinned` 行数加上各分量
匹配数独立复核——人工绑定确实可以比原图最大匹配数更少。

## 原子提交、回滚与 generation

- import/decide 都是单 `BEGIN IMMEDIATE` 事务：解析、重放判定、来源/倒排/边
  写入、分量合并、推断重写、决定日志、绑定、generation 与 maximum_pairs 要么整体
  提交要么整体回滚。测试通过 `fail_after_steps` 在“推断写到一半”处抛错
  （`tests/vc_test_linkage.py::test_failure_in_the_middle_of_inference_rolls_every_layer_back`），
  证明重开后各层与 generation 与批前逐行相等，重试结果与无故障运行逐字节一致。
- 重放判定基于解析后规范载荷（别名/附言排序、键排序的 JSON 文本）：同载荷忽略、
  异载荷冲突整批拒绝。决定日志的载荷是“解绑后旧 pin 重试”不会复活绑定的依据。
- `generation` 只在真正写入新来源或新决定时加一；纯重放批次不改任何行，也不加一。
  来源日志 `imports` 与 `decisions` 一样只追加，每个改变 generation 的导入批次恰有
  一行，批次内的来源、边与推断以同一事务与它一起可见；纯重放不写日志行。
- report 在 WAL 只读连接的一个读快照里流式写出 `inference` 两路有序扫描，
  与写者互不阻塞；并发观察者只看到完整旧代或完整新代（线程内观察者与四进程
  并发报告测试各一）。

## 局部性证据

`tests/vc_test_locality.py` 与 `tests/vc_test_scale.py` 用存储层行计数与 SQL
轨迹证明：

- 同一“新增一笔支付 + 一笔到账并绑定”的局部操作，在 10 个无关分量与 300 个
  无关分量（边数差 20 倍以上）的库上，`payments_read / credits_read /
  references_read / aliases_read / edges_read` 计数逐项相同；decide 的
  `comp_vertex_read=2`、`edges_read=2`、`inference_written=2` 也完全相同。
- 局部操作 SQL 中不存在无条件 `SELECT`、不存在 `payments × credits` 笛卡尔
  探测（所有候选发现都走倒排表 + JOIN 记录表），不存在全表聚合；report 只做
  两次 `FROM inference ... ORDER BY vertex_id` 顺序扫描加两条 `meta` 读取。
- 20 万顶点 / 100 万候选边库上（`VC_SCALE=1`）局部导入仍约 5 ms、读取计数与
  小库相同；全量 report 约 1.8 s 流式输出。
- 单个最大分量（4000 顶点 / 20000 边）在一次导入中构建并分类；再增大就整批
  拒绝且不留来源记录。

## 备选设计及其代价

1. **每次 report 现算匹配（无持久推断表）**：写侧最省，但报告要对全库每个分量
   重跑最大匹配与全解判定；20 万顶点、100 万边时每次报告都是全库 O(E√V) 重算，
   并发报告互相抢 CPU，且无法在 report 中流式输出（必须先物化整张结论）。本仓
   选择把推断作为派生层与事实同事务维护，换取报告只做一次顺序扫描。
2. **绑定即删边（把绑定边从候选图移除）**：会让“解绑后恢复全歧义”与“绑定不改变
   原始候选”难以保证，且同一对端再次 pin 的历史无法从图结构还原。本仓保留原始
   边、只在分类时屏蔽绑定端点，代价是每次分类要跳过已绑定顶点。
3. **全局单分量索引（或每次增量后全局重算分量）**：失效传播简单但写放大为
   O(全库)，任何局部导入都会重算所有无关分量。本仓按连通分量维护，写放大为
   O(受影响分量)；代价是桥接旧分量时必须合并并重算合并后的整分量（这正是正确性
   所要求的）。
4. **按边缓存“是否在某个最大匹配中”**：单边判定可以通过删边重跑一次匹配得到，
   但那是“每条候选边一次完整最大匹配”，在 2 万边分量上是 2 万次匹配；本仓的
   X/Y/SCC 判定一次扫描就得到全部顶点的对端集合与可未匹配标志。
5. **枚举全部最大匹配再统计**：k 个 4-环的分量有 2^k 个最大匹配，指数级；契约
   明确禁止，本仓的判定结构在结构上排除了这种可能。

## 一个容易写错的分量合并细节

并查集同时承载“旧分量 id”（非负）与“本批新顶点”（负的虚拟节点）。**同一旧分量
有多个成员顶点参与本批时，第一次把该分量 id 放进森林后，后续成员不能再把它的
parent 重置为自身**，否则会把本批先前的 union 悄悄拆开，导致分量行与
`comp_vertex` 归属不一致、推断时读到跨分量边。实现用 `parent.setdefault(node,node)`
只在首次登记，并有一条回归测试（两个旧分量 + 三个新顶点同时桥接）和随机
不变量检查：40 轮随机导入后，每个分量行的 `vertex_count`/`edge_count` 与
`comp_vertex`、`edges` 逐项相等，且没有任何边跨分量。
