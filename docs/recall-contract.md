# 原料批次谱系与召回契约（venue-recall）

独立于场地占用（venue-occupancy）与场地电能（venue-energy）管道的第三条管线：
按原料批次记录收货（receive）、混匀（blend）与送达（serve），以**精确有理数**维护
每个批次对原始批次的组成比例；召回任一原始批次即永久隔离其全部后代的剩余库存，
并输出受影响的送达与库存报告。Python 3.11+、仅标准库，状态完全位于一个 SQLite
文件中。本管道不从预订量、容量或电表读数推算任何原料数量。

```sh
venue-recall init   --db D --source J     # 从 {"venues":[...]} 建档
venue-recall ingest --db D --source J     # 原子应用一批 {"operations":[...]}
venue-recall recall --db D --source J     # 把 {"roots":[...]} 并入永久召回集合
venue-recall report --db D --output J     # 同一读快照输出影响报告
```

成功退出 0；任何输入或计算错误退出 2，数据库与旧报告原样保留。输入 JSON 最多
8MB，拒绝重复 JSON 键与非有限数；标识符为非空、去空白、长度 ≤128、无控制字符的
字符串。`report --output` 不得等于 `--db`，拒绝符号链接输出；输出经同目录临时文件
原子替换，失败保留旧文件。

## init

`--source` 严格为 `{"venues":[场地ID,...]}`：1..100 个场地、不得重复、不得含其他键。
数据库文件已存在且非空时拒绝 init（退出 2）；空文件可被采用。init 在兄弟临时文件中
单事务建库后原子改名就位，失败不留半成品。

## ingest

`--source` 严格为 `{"operations":[...]}`，每批最多 20000 项，**按数组顺序解释**，
整批在一个事务里提交或回滚。每项恰含下列三种形状之一（不得多字段、不得少字段）：

```json
{"op_id":"r1","type":"receive","lot_id":"flour-a","grams":1000}
{"op_id":"b1","type":"blend","inputs":[{"lot_id":"flour-a","grams":400}],
 "outputs":[{"lot_id":"dough-x","grams":400}]}
{"op_id":"s1","type":"serve","lot_id":"dough-x","grams":150,"venue":"hall"}
```

- `op_id`：全局唯一标识符。同一 `op_id` 的**解析后完整载荷**相同则该条为重放、
  忽略（不再次扣库存）；不同即冲突，整批拒绝。blend 的重放比较保留 inputs/outputs
  的数组顺序，顺序不同即不同载荷。已被接受的操作即使其批次后来被召回，相同重试
  仍被忽略（成功退出）。
- `grams`：1..10^12 的整数，拒绝布尔值、浮点与字符串。
- `receive` 创建一个原始批次；`lot_id` 在全库从未出现过（耗尽的批次也不能重建）。
- `blend` 的 `inputs`/`outputs` 各为 1..100 项 `{lot_id,grams}`；每个列表内批次 ID
  唯一；输入批次必须已存在、未隔离、剩余库存充足；输出 ID 在全库从未出现；
  **输入质量之和等于输出质量之和**。混匀表示输入被消耗、输出批次以同一混合物
  组成创建（同一 blend 的全部输出共享同一组成比例）。
- `serve` 的 `venue` 必须是 init 登记的场地；批次必须存在、未隔离、库存充足。
- 批中任一操作失败，整批回滚（包括批内靠前的合法操作）。

## 库存账务与谱系

数据库保存：每次消耗事件与其产出操作的关联（`consumption` 表 + 批次的创建操作）、
每批剩余克数（`lots.remaining_grams`，与消耗同事务更新）、每批对原始批次的精确
组成（`composition` 表，分子/分母为十进制整数字符串）。

组成比例的定义：原始批次对自身比例为 1；blend 的每个输出对某原始批次的比例等于
各输入消耗克数乘其该比例之和，再除以本次总输入克数；后续部分消耗沿用该输出比例
（消耗不改变组成）。计算全程使用任意精度有理数：禁止浮点、逐步取整、按克展开与
枚举祖先路径；分叉后合流的各支路真实质量在汇合处累加一次。

## recall

`--source` 严格为 `{"roots":[原始批次ID,...]}`。每个根必须是 `receive` 创建的已知
批次；未知 ID 或 blend 产物整批拒绝（退出 2，无任何根被加入）。所有新根**原子**
并入永久召回集合；请求内重复 ID 与重复请求无额外作用。

召回生效规则：若某批次对任一已召回原始批次的组成比例大于 0，其**全部**剩余库存
立即隔离（不能只扣掉归因质量后继续用），后续 blend 与 serve 都不得消耗该批次；
已送达记录保留并进入影响报告。该判定适用于任意多层混料后才发出的召回。召回集合
的判定与写入都在数据库写事务内完成：recall 与 ingest 由数据库写锁串行化，两个
进程竞争召回与送达时，结果必等价于某一个完整串行顺序。

## report

`report --db D --output J` 在同一读快照内输出，重复执行不改变库存：

```json
{
  "recalled_roots": ["flour-b"],
  "deliveries": [
    {"op_id": "s1", "venue": "hall", "lot_id": "dough-x", "grams": 150,
     "affected_grams": {"numerator": "50", "denominator": "1"}}
  ],
  "stock": [
    {"lot_id": "dough-x", "remaining_grams": 150,
     "affected_grams": {"numerator": "50", "denominator": "1"}, "quarantined": true}
  ]
}
```

- `recalled_roots`：全部已召回根，按 ID 排序。
- `deliveries`：仅受影响的 serve 操作，按 `op_id` 排序，含
  `op_id,venue,lot_id,grams,affected_grams`。
- `stock`：仅尚有剩余且受影响的批次，按 `lot_id` 排序，含
  `lot_id,remaining_grams,affected_grams,quarantined:true`。
- `affected_grams` = 该行克数 × 全部已召回根的组成比例之和，输出**最简非负有理数**
  `{numerator:"十进制整数",denominator:"正十进制整数"}`。多个根、多条路径各自的真实
  质量只累加一次，绝不按根或按路径把整批重复计数；零贡献行不输出。

## 可复现样例

`fixtures/recall/` 三个文件手工构造：flour-a 1000 g 与 flour-b 600 g 收货；
400+200→600 混成 dough-x 并向 hall 送达 150 g；dough-x 300 g 与 flour-a 100 g
混成 dough-y 400 g 并向 annex 送达 80 g；随后召回 flour-b。预期报告逐字节确定：

```json
{
  "deliveries": [
    {"affected_grams": {"denominator": "1", "numerator": "50"},
     "grams": 150, "lot_id": "dough-x", "op_id": "s1", "venue": "hall"},
    {"affected_grams": {"denominator": "1", "numerator": "20"},
     "grams": 80, "lot_id": "dough-y", "op_id": "s2", "venue": "annex"}
  ],
  "recalled_roots": ["flour-b"],
  "stock": [
    {"affected_grams": {"denominator": "1", "numerator": "50"},
     "lot_id": "dough-x", "quarantined": true, "remaining_grams": 150},
    {"affected_grams": {"denominator": "1", "numerator": "80"},
     "lot_id": "dough-y", "quarantined": true, "remaining_grams": 320},
    {"affected_grams": {"denominator": "1", "numerator": "400"},
     "lot_id": "flour-b", "quarantined": true, "remaining_grams": 400}
  ]
}
```

flour-b 质量守恒：库存 50+80+400 加送达 50+20 恰为收货 600 g（dough-x 组成
2/3·flour-a+1/3·flour-b，dough-y 组成 3/4·flour-a+1/4·flour-b）。
`tests/vr_test_cli.py` 逐字段核对该报告。

## 明确的非目标

不做真实库存审批、供应商管理或保质期建模；场地集合在 init 后不可变；不提供召回
撤销或历史压缩（事实与召回集合只增）；不依赖外部服务或网络；不修改占用/电能
管道的库表来存放本业务。
