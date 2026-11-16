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
