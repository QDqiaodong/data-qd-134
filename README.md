# data-qd-134 · 场地占用容量审计

Half-open booking interval sweep and capacity exceedance evidence，外加一个本地 SQLite
修订数据集：运营补发旧预订、撤回和跨场地改期后，导出最新一次完整提交对应的精确占用报告。
业务输入、计算、数据集契约与资源边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库（`sqlite3` 随 CPython 提供）；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/venue-occupancy --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -t . -v
```

未安装时也可从源码树运行：`PYTHONPATH=src python3 -m venue_occupancy.cli ...`。

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/venue_occupancy/model.py 为业务模型，sweep.py 为扫描算法，common.py 提供受限JSON及原子发布；
revisions.py 校验修订、materialize.py 局部维护分段、store.py 是 SQLite 表结构与打开方式、
dataset.py 衔接三段事务，cli.py 衔接命令行。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 修订数据集（init / apply / export）

无子命令的 `--source/--output` 调用与既有报告语义完全不变；以下三个子命令使用同一个 SQLite 文件。

```sh
# 1) 从原契约 JSON 建档：冻结 horizon 与 venues，初始 bookings 记为 rev=0
.venv/bin/venue-occupancy init --db run/audit.db --source fixtures/input.json

# 2) 应用一批修订 {"revisions":[...]}，整批一个事务
.venv/bin/venue-occupancy apply --db run/audit.db --source batch.json

# 3) 在一次读快照内导出 {generation,result}，result 沿用原模型格式
.venv/bin/venue-occupancy export --db run/audit.db --output run/latest.json
```

修订记录只有两种严格形状：

```json
{"id":"B","rev":7,"op":"upsert","venue":"hall","start":5,"end":15,"quantity":2}
{"id":"C","rev":9,"op":"delete"}
```

`rev` 为 1..10^12 的整数；`upsert` 的 venue/start/end/quantity 与 delete 的未知 id 都沿用原校验，
只有 `delete` 允许指向从未出现的 id。每批最多 20000 项、8MB。一个 id 只有最高 rev 有效：
最高版本若是 delete 则该 id 不贡献占用；低版本永久留存但不会复活或覆盖高版本。
同一 `(id,rev)` 的完整字段相同即为重放，不同即为冲突；冲突即使指向过期版本也整批拒绝。
批内顺序与乱序都不影响最终有效集合。新增事实使 `generation` 加一，纯重放不变，初始为0。

`export` 输出沿用原报告结构，外面包一层 `generation`：

```json
{"generation":3,"result":{"horizon":{"start":0,"end":20},"venues":[...]}}
```

它沿用原有的安全发布路径（临时文件 + 原子替换、拒绝符号链接、失败退出2并保留旧文件），
并从一个 SQLite 读快照顺序遍历全部场地，因此观察者只会看到完整的旧代或完整的新代。

## 运维要点

- `facts` 保留全部修订（验收规模 200 万条），`effective` 每个 id 一行；两者只增不重算历史。
- 分段覆盖完整 horizon、相邻等占用段唯一合并，空场地也保留一段零占用。
- `segments`、`venue_levels`、`venue_metrics` 是派生缓存，与事实和 `generation` 同事务提交；
  进程重启后不重建，直接沿用。init 时按场地各扫一次有效预订，apply 时不再有场地级扫描。
- 单批只改少数 id 时，apply 按 `(id,rev)`、id、`(venue,start)` 主键定位，只访问本批 id 的事实、
  旧新区间并集内的分段及必要邻居，并只对该场地的占用直方图做增量更新；不按 tick 展开，
  也不加载全体修订、有效预订或整条时间轴。export 可以顺序遍历输出。
- 单写入者、WAL 模式；并发只允许“若干读快照导出 + 一个写事务”，不承诺多写者。

## 业务与样例结果

半开预订区间裁剪到明确统计范围；hall峰值4、超容5 tick，预订总量55 unit-tick，空场地也保留零占用段。

# 场地电能结算（venue-energy audit）

独立于占用管道的第二条管线：**只用电表寄存器读数**结算，不从预订量或容量推算用电，
按场地、UTC 自然月输出实测 Wh、成本微单位、碳排 mg 与数据完整性。Python 3.11+、仅标准库，
输入输出契约见 [energy-contract.md](docs/energy-contract.md)，排序/去重结构与资源论证见
[energy-impl.md](docs/energy-impl.md)。旧 `venue-occupancy` 命令与报告保持原样，两个包互不 import。

```sh
.venv/bin/venue-energy audit \
  --meters fixtures/energy/meters.json \
  --readings fixtures/energy/readings.csv \
  --tariffs fixtures/energy/tariffs.json \
  --output run/energy-report.json
# 未安装时：PYTHONPATH=src python3 -m venue_energy.cli audit ...
```

要点：相邻读数按 `E=R1-R0+k*modulus_wh` 枚举非负 k，结合 `max_watts` 功率界，
**恰有一个可行解释才结算**；零个或多个可行解释的区间标记为不确定、排除其电量，
并把覆盖到的每个 UTC 月标 `complete=false`，绝不默认零回绕/最少回绕。有效区间按 UTC
月界与费率边界切片，整数 Wh 按片时长比例下取整、余数按时间先后逐 Wh 分配且各片之和守恒；
成本/碳排按月以精确分数累加后四舍五入。重复 read_id 忽略、冲突 read_id 或同表同时刻
异读数整次拒绝。500 万行下内存不随行数增长（实测峰值约 33 MiB，上限 128 MiB），
外部排序 run 落临时目录并在结束/异常时清理，输出原子替换、失败保留旧文件。

```sh
.venv/bin/python -m unittest tests.ve_test_engine tests.ve_test_oracle \
  tests.ve_test_contract tests.ve_test_cli tests.ve_test_memory -v
VE_MEMORY_FULL_SCALE=1 .venv/bin/python -m unittest tests.ve_test_memory -v   # 500 万行全量
```

测试含独立小规模全量 oracle（`tests/ve_test_oracle.py`，以 `Fraction` 独立重算逐项核对，
并强制极小缓冲触发多级归并）、乱序/重复/冲突、零与多回绕、跨费率跨月余数分配、
空/重叠费率拒绝，以及子进程 ru_maxrss 大规模内存对照（`tests/ve_memory_probe.py`）。

# 原料批次谱系与召回（venue-recall）

独立于占用与电能管道的第三条管线：按原料批次记录收货、混匀与送达，以**精确有理数**
维护每个批次对原始批次的组成比例；召回任一原始批次即永久隔离其全部后代剩余库存，
并输出受影响送达与库存报告。Python 3.11+、仅标准库，输入输出契约见
[recall-contract.md](docs/recall-contract.md)，持久化/计算结构与备选设计代价见
[recall-impl.md](docs/recall-impl.md)。旧 `venue-occupancy`、`venue-energy` 命令与
报告保持原样，三个包互不 import，本管道不从预订或电表数据推算原料。

```sh
.venv/bin/venue-recall init   --db run/recall.db --source fixtures/recall/init.json
.venv/bin/venue-recall ingest --db run/recall.db --source fixtures/recall/operations.json
.venv/bin/venue-recall recall --db run/recall.db --source fixtures/recall/recall.json
.venv/bin/venue-recall report --db run/recall.db --output run/recall-report.json
# 未安装时：PYTHONPATH=src python3 -m venue_recall.cli init|ingest|recall|report ...
```

要点：receive/blend/serve 三种操作每批 ≤20000 项、按数组顺序解释、整批提交或回滚；
相同 op_id 重放忽略、冲突整批拒绝，重试不二次扣库存；混匀输出的组成以任意精度
分数逐批物化（分母可超 64 位），禁止浮点、按克展开与祖先路径枚举；召回与领用在
数据库写事务内串行判定，竞态等价于某一完整串行顺序；报告在同一读快照输出，
受影响质量输出最简分数，原子替换且失败保留旧文件。样例预期报告（flour-b 质量
守恒：库存 50+80+400 加送达 50+20 恰为 600 g）见契约文档。

```sh
.venv/bin/python -m unittest tests.vr_test_cli tests.vr_test_inventory \
  tests.vr_test_genealogy tests.vr_test_recall tests.vr_test_concurrency \
  tests.vr_test_locality -v
```

测试含真实 CLI 子进程、独立有理数 oracle（同源分叉合流、超 64 位分母、随机 op 流
逐项核对）、30 层菱形反指数路径证明、库存逐项验收（部分领用/多输出/耗尽再建/
超量/批中回滚）、召回联动（隔离整批、双根不重复计数、顺序无关、召回后重放忽略）、
线程与进程双组召回-送达竞速、并发报告快照与提交前故障重试。

# 支付与银行到账核销（venue-clearing）

独立于占用、电能与召回的第四条管线：导入支付（带别名）与银行到账（带附言引用），
仅当**场地、币种、金额全等且别名与附言项有交集**时生成候选边；在人工绑定与
候选图**全部最大基数匹配**的解空间上分类每个顶点，绝不把一次求得的匹配当成唯一
核销结果，也不靠记录行序或任取一个对端。Python 3.11+、仅标准库，契约见
[clearing-contract.md](docs/clearing-contract.md)，图索引、分量维护与全解空间判定
结构及备选设计代价见 [clearing-impl.md](docs/clearing-impl.md)。旧
`venue-occupancy`、`venue-energy`、`venue-recall` 命令与报告保持原样，四个包
互不 import，新业务不写入旧库。

```sh
.venv/bin/venue-clearing init   --db run/clearing.db
.venv/bin/venue-clearing import --db run/clearing.db --source fixtures/clearing/payments.json
.venv/bin/venue-clearing import --db run/clearing.db --source fixtures/clearing/credits.json
.venv/bin/venue-clearing decide --db run/clearing.db --source fixtures/clearing/pin.json
.venv/bin/venue-clearing report --db run/clearing.db --output run/clearing-report.json
# 未安装时：PYTHONPATH=src python3 -m venue_clearing.cli init|import|decide|report ...
```

要点：每文档 ≤2000 条记录、8MB，严格对象形状，拒绝未知字段、重复 JSON 键、
非三位大写币种与非整数金额；两类 ID 各自全库唯一，同载荷重放忽略、异载荷整批
拒绝；导入与决定各在单事务内提交或回滚。人工 pin/unpin 后重算**整个受影响原始
候选分量**，新增到账桥接两个旧分量时合并并重算，绑定不删原始边，解绑后旧 pin
重试不会重新绑定。`maximum_pairs` = 绑定数 + 剩余图最大匹配数；`generation`
只随真正写入的新来源/新决定加一。报告在单一读快照流式输出，原子替换且失败保留
旧文件。

```sh
.venv/bin/python -m unittest tests.vc_test_source tests.vc_test_classify \
  tests.vc_test_decisions tests.vc_test_linkage tests.vc_test_locality \
  tests.vc_test_cli tests.vc_test_scale -v
VC_SCALE=1 .venv/bin/python -m unittest tests.vc_test_scale -v    # 20 万顶点 / 100 万边
```

测试含与**独立穷举 oracle** 的对拍（3×3 以内穷举全部图、7×7 随机图、随机
导入/绑定序列），2^400 个最大匹配的构造证明不枚举解空间，来源层的两种到达顺序
/别名乱序重试/同额异币种异场地/批末冲突，决定层的占用冲突/解绑/旧 pin 重试/
批中回滚，联动层的 2×2 歧义→绑定→forced→解绑恢复、桥接合并、写到一半故障的
全层回滚与重开，以及局部操作行计数在无关分量增长时逐项不变（20 万顶点库上局部
导入仍约 5 ms）。
