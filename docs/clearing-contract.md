# 支付与银行到账核销契约（venue-clearing）

独立于场地占用（venue-occupancy）、场地电能（venue-energy）与原料召回
（venue-recall）的第四条管线：导入支付（带支付别名）与银行到账（带附言引用），
在**场地、币种、金额全等且别名与附言项有交集**时生成候选边，并在人工绑定与
候选图全部最大基数匹配的解空间上分类每个顶点。Python 3.11+、仅标准库，
状态完全位于一个 SQLite 文件中。本管道不从占用、电能或原料数据推算任何核销。

```sh
venue-clearing init   --db D                     # 建立空库
venue-clearing import --db D --source J          # 原子应用 {"payments":[...],"credits":[...]}
venue-clearing decide --db D --source J          # 按数组顺序应用 {"decisions":[...]}
venue-clearing report --db D --output J          # 同一读快照输出分类报告
```

成功退出 0；任何输入或计算错误退出 2，数据库与旧报告原样保留。同额到账的附言
**可能只保留支付别名**，因此收入归属只能由候选边与显式人工绑定决定，不能靠记录
行序、先到先得或任取一个匹配。

## init

`init --db D` 建立空库：`meta`（schema 版本、generation=0、maximum_pairs=0）、
来源表、倒排表、候选边表、分量表、推断表、绑定表与决定日志表。数据库文件已存在
且非空时拒绝（退出 2）；空文件可被采用。init 在兄弟临时文件中单事务建库后原子
改名就位，失败不留半成品。

## import

`--source` 严格为 `{"payments":[...],"credits":[...]}`，两键都必须出现（可为空
数组），不得含其他键；每文档 `payments` 与 `credits` 合计最多 2000 条记录、
最多 8 MB，拒绝重复 JSON 键、非有限数值与未知字段。

```json
{"payment_id":"P-100","venue":"hall","currency":"USD","amount_minor":12000,
 "aliases":["REF-A","REF-B"]}
{"credit_id":"C-1","venue":"hall","currency":"USD","amount_minor":12000,
 "references":["REF-A"]}
```

- payment 恰含 `payment_id,venue,currency,amount_minor,aliases`；credit 恰含
  `credit_id,venue,currency,amount_minor,references`。
- `payment_id`/`credit_id`、`venue`、别名与附言项沿用本仓标识符校验：非空、
  去空白、长度 ≤128、无控制字符的字符串。
- `currency` 恰为三个大写 ASCII 字母；`amount_minor` 是 1..10^12 的整数，
  拒绝布尔值、浮点与字符串。
- `aliases`/`references` 各为 1..100 个**互异**字符串；数组顺序无意义，重试
  比较按排序后的集合进行。
- 每个 ID 在**各自的类型内全库唯一**。同一 ID 解析后字段相同即为重放、忽略；
  字段不同（含批内重复 ID 但载荷不同）即冲突，整批拒绝。
- 支付与到账可先后到达（任意顺序、分多批）。

候选边规则：仅当两记录 `venue`、`currency`、`amount_minor` 三者全等，且
`aliases` 与 `references` 有非空交集时存在一条候选边。多个别名/附言项同时命中
只算**一条**边。候选性是两条记录字段的纯函数，因此新增一条记录只会通过倒排
索引发现它自己的边，不会改变既有记录之间的边。

持久化：来源记录（含规范载荷用于重放）、别名→支付与附言→到账两张倒排表、
候选边表、原始候选连通分量及其规模、推断结果，以及一个只追加的来源日志
（每个真正新增事实的批次一行，标明它产生的 generation）。整批在一个
`BEGIN IMMEDIATE` 事务中提交或回滚；批中任一条非法或与库中状态冲突则全部撤销。

## decide

`--source` 严格为 `{"decisions":[...]}`，最多 2000 项，**按数组顺序解释**：

```json
{"decision_id":"pin-1","op":"pin","payment_id":"P-100","credit_id":"C-1"}
{"decision_id":"unpin-1","op":"unpin","payment_id":"P-100"}
```

- pin 恰含 `decision_id,op,payment_id,credit_id`；unpin 恰含
  `decision_id,op,payment_id`；`op` 必须是操作名本身。
- `decision_id` 全局唯一。同 ID 同完整载荷重试先忽略；异载荷整批冲突。
  **已解绑的旧 pin 重试不会重新绑定**：日志里该 decision_id 的载荷即重放依据。
- pin 必须是现存候选边，且两端均未被**其他**绑定占用；同一已绑定对再次 pin
  （新的 decision_id）合法。人工绑定是外部确认，可以降低原图的最大配对数，
  绝不因“推断更偏好别的配对”而拒绝。
- unpin 要求支付存在；没有绑定也合法（只记录决定）。
- 批中任一决定失败（含靠前的合法决定之后出现非法决定）整批回滚。

## 核销推断

推断边界由当前**原始候选图**与人工绑定共同决定：

1. 固定全部人工绑定，把绑定两端从匹配图中移除；
2. 在剩余图上考虑**所有最大基数匹配**（不是某一次求得的匹配）；
3. 对每个未绑定顶点输出：
   - `possible_peers`：至少出现在一个最大匹配中的对端 ID，升序；
   - `can_be_unmatched`：至少在一个最大匹配中未配对时为 true。
4. 状态：`possible_peers` 为空 → `unmatched`；只有一个对端且不能未配对 →
   `forced`；其余 → `ambiguous`。绑定两端状态为 `pinned`，对端唯一且
   `can_be_unmatched=false`。
5. `maximum_pairs` = 绑定数 + 剩余图最大匹配数。

每笔支付与每笔到账至多配对一次；所有顶点（含孤立点）都要输出。一次求得的
最大匹配只是解空间的一个样本，绝不能直接当作核销结果。

人工绑定或新事实会影响整个**原始候选连通分量**：pin/unpin 后重新分类该分量中
的全部顶点，而不只是两行绑定；导入把两个旧分量桥接成一个时，合并后整分量重新
分类，过期推断不会残留。绑定不删除原始候选边。

## 持久化一致性与 generation

来源/决定日志、倒排、候选边、分量、绑定、推断与 generation 在同一事务内可见。
有新来源（至少写入一条新 payment/credit）或新决定（至少一条非重放决定）的成功
批次使 `generation` 加一；纯重放批次不变；初始为 0。`maximum_pairs` 同样随批次
增量维护。

## report

`report --db D --output J` 在一个读快照内流式输出：

```json
{"generation":2,"maximum_pairs":2,
 "payments":[{"id":"P-100","status":"forced","possible_peers":["C-1"],"can_be_unmatched":false}],
 "credits":[{"id":"C-1","status":"forced","possible_peers":["P-100"],"can_be_unmatched":false}]}
```

- 两个数组分别按各自 `id` 升序；每行恰含 `id,status,possible_peers,can_be_unmatched`；
  `possible_peers` 升序。
- 输出经同目录临时文件 + 原子替换；`--output` 不得等于 `--db`，拒绝符号链接；
  失败退出 2 并保留旧文件。
- 报告中任意一行缺失都说明库不完整；所有曾导入的顶点（即使从未有候选边）都在
  `unmatched` 行中列出。

## 规模与局部性边界

长期库可有 20 万顶点、100 万候选边。每个**原始候选连通分量**最多 4000 顶点、
20000 条边；新记录或新边使某分量超限时整批拒绝（不落任何来源记录）。超出这些
上界的库不受支持。

import/decide 只读取本批身份、倒排候选命中、受影响分量及其顶点/边，不扫描无关
来源、图或推断；工作内存随本批与最大受影响分量增长，不随全库增长。report 可
流式遍历推断表。单写者（WAL 下由写锁串行化）与多个并发报告允许交错，报告只能
看到完整旧代或完整新代。

## 可复现样例

`fixtures/clearing/` 手工构造，无外部数据：

- `payments.json` / `credits.json`：hall/USD/12000 的 P-100（别名 REF-A、REF-B）
  与 P-200（REF-B），hall/EUR/12000 的 P-300（REF-C）；到账 C-1(REF-A)、
  C-2(REF-B,REF-A)、C-3(REF-C)、annex/EUR 的 C-4(REF-C)、金额 10000 的
  C-5(REF-A) 与金额 999 的 C-9(REF-Z)。候选边恰为 P-100–C-1、
  P-100–C-2、P-200–C-2（币种/场地/金额不同者被排除，多项交集只算一条边）。
- `pin.json` 绑定 P-100–C-1；`unpin.json` 解绑 P-100。

导入后（generation=2，maximum_pairs=2）：

| 顶点 | 状态 | possible_peers | can_be_unmatched |
| --- | --- | --- | --- |
| P-100 | forced | C-1 | false |
| P-200 | forced | C-2 | false |
| P-300 | unmatched | — | true |
| C-1 | forced | P-100 | false |
| C-2 | forced | P-200 | false |
| C-3/C-4/C-5/C-9 | unmatched | — | true |

绑定 P-100–C-1 后两者变 `pinned`，P-200/C-2 仍 `forced`，maximum_pairs 仍为 2；
解绑后恢复上表（generation=4）。`tests/vc_test_cli.py` 逐字段核对。

## 明确的非目标

不做真实收单/银行网关对接、退款拆分、部分金额核销、多币种换算或会计过账；
不提供人工绑定的审计审批流；不删除或压缩来源与决定历史；不依赖外部服务或网络；
不修改占用/电能/召回管道的库表，也不从它们的库读取或写入任何数据。
