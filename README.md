# 电表累计读数 -> 区间用电量 API

把电表的**累计读数**（Wh）增量转成相邻读数之间的**区间用电量**，供负荷分析使用。
FastAPI + PostgreSQL，区间由数据库函数做持久化的增量维护；换表导致的缺测段可凭
**换表工单**（拆除读数/装机读数）持久化补算成多个子段，换表前后用电分开看。

## 1. 业务规则

### 1.1 读数与区间

- 读数字段：`id`、`meter_no`（电表号）、`read_at`（UTC 时刻）、`reading`（0～999999 整数 Wh）、
  `replaced`（换表标记）；同表同时刻唯一（数据库唯一约束保证）。
- 同一电表按时刻排序，相邻两条读数 `(a, b)` 形成**左开右闭**区间 `(a.read_at, b.read_at]`：
  - 首条读数之前**无区间**；
  - 若右端读数 `b.replaced = true`（换表后首读），区间电量**缺测** `energy_wh = NULL`；
  - 否则 `energy_wh = b.reading - a.reading`；差值为负时按 **1,000,000 单次回绕**
    （即 `+1000000`）。
- 补录 / 更正 / 删除读数都会**增量**修正相邻区间，不残留旧的跨越段；
  更改电表号会同时修正**旧表与新表**两只电表。

### 1.2 换表工单与缺测段补算

- 工单按 `meter_no` + UTC `replaced_at`（换表时刻）保存
  `removal_reading`（拆除读数，旧表末读）与 `install_reading`（装机读数，新表首读），
  均为 0～999999 整数 Wh；**同表同刻再次提交即更正**（UPSERT）。
- 仅当换表时刻**严格落在缺测段两端之间**（`start < replaced_at < end`，等于端点不参与）
  的工单才参与该段补算。
- 对一条缺测段，适用工单按换表时刻**逐次切开**：拆除读数结束上一段，装机读数开启下一段；
  最外两端沿用**原采样读数**；每个子段沿用 1,000,000 **单次回绕**。
  `k` 个工单把一条缺测段切成 `k+1` 个补算子段，子段返回起止时刻、Wh 和所用工单时刻
  （端点取自原采样读数时对应工单时刻为 `null`）。
- **没有工单就保留缺测**：`backfill_segments` 不产生行，原始缺测段仍为 `energy_wh = NULL`。
- 补算子段**持久化**在 `backfill_segments`。登记/更正/删除工单，或通过读数 API、
  SQL 函数增删改读数时，**只更新受影响段**；补录使工单换段或脱离缺测段时，
  旧补算与源数据**同事务撤下**（外键级联 + 显式重建双保险，外键约束可延迟到提交时检查）。

## 2. 快速启动（Docker Compose）

```bash
docker compose up --build
```

- API： http://localhost:8000  ，交互式文档： http://localhost:8000/docs
- PostgreSQL： localhost:55438（容器内 5432），库 `meterdb`，用户/密码 `postgres/postgres`
- 启动时 `api` 容器会等待数据库就绪，再幂等执行 `sql/` 下迁移，然后启动 uvicorn。
- 数据存放在命名卷 `pgdata`，**重启保留**。

健康检查：

```bash
curl http://localhost:8000/health
```

## 3. HTTP 接口（详见 /docs）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/readings` | 新增读数（补录亦可，自动拆分区间） |
| PUT | `/readings/{id}` | 更正读数（含改电表号，修正两只表） |
| DELETE | `/readings/{id}` | 删除读数（相邻区间自动合并） |
| GET | `/readings?meter_no=...` | 查读数 |
| GET | `/intervals?meter_no=...` | 查区间电量 |
| POST | `/work-orders` | 登记/更正换表工单（同表同刻重提即更正；新建 201，更正 200） |
| GET | `/work-orders?meter_no=...` | 查换表工单 |
| DELETE | `/work-orders/{id}` | 删除工单（受影响缺测段重建，无剩余工单则恢复缺测） |
| GET | `/supply?meter_no=...` | **同一提交快照**返回读数、原始缺测、补算子段、工单 |
| GET | `/meters` | 电表清单 |

写入示例：

```bash
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"meter_no":"M-001","read_at":"2026-01-01T00:00:00Z","reading":999900}'
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"meter_no":"M-001","read_at":"2026-01-01T01:00:00Z","reading":100}'   # 回绕 -> 200 Wh
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"meter_no":"M-001","read_at":"2026-01-01T02:00:00Z","reading":500,"replaced":true}' # 缺测
```

换表工单 + 供数（原始缺测与补算子段分开返回）：

```bash
# 登记换表工单: 01:30 换表, 拆除 999800 Wh, 装机 100 Wh (首次 201; 同表同刻再提即更正 200)
curl -X POST http://localhost:8000/work-orders -H 'Content-Type: application/json' \
  -d '{"meter_no":"M-001","replaced_at":"2026-01-01T01:30:00Z",
       "removal_reading":999800,"install_reading":100}'

# /supply 在同一个 REPEATABLE READ 快照内分开返回:
#   missing_intervals  原始缺测值 (energy_wh=null 的换表缺测段)
#   backfill_segments  持久化补算子段 (起止/Wh/所用工单时刻; 无工单的缺测段不出现)
#   work_orders        本表工单 (补算溯源)
curl 'http://localhost:8000/supply?meter_no=M-001'
```

上例缺测段 `(01:00,02:00]` 被切成两个补算子段（最外两端为原采样读数）：

| 子段 | 起 → 止 | 起读数 → 止读数 | 电量 | 工单时刻 |
| --- | --- | --- | --- | --- |
| 0 | 01:00 → 01:30 | 999500 → 999800（拆除） | 300 Wh | 左端 `null`，右端 01:30 |
| 1 | 01:30 → 02:00 | 100（装机）→ 200 | 100 Wh | 左端 01:30，右端 `null` |

## 4. 独立 SQL 示例（READ COMMITTED，psql 直连即可）

API 与手工 SQL 走的是同一套数据库函数，行为完全一致：

- [`docs/examples/01_backfill.sql`](docs/examples/01_backfill.sql)：补录历史读数，旧区间自动拆分。
- [`docs/examples/02_meter_replace.sql`](docs/examples/02_meter_replace.sql)：换表缺测 + 改电表号修正两只表。
- [`docs/examples/03_concurrency.sql`](docs/examples/03_concurrency.sql)：同表并发串行、异表并发不阻塞。
- [`docs/examples/04_work_order_backfill.sql`](docs/examples/04_work_order_backfill.sql)：
  工单登记/更正/删除、严格边界、缺测段逐次切开的补算子段与巡检。

## 5. 并发与一致性设计（要点）

- 每个写事务先在 `meters` 表上按 `meter_no` **排序加行锁**（`SELECT ... FOR UPDATE`），
  之后才操作该表的读数/区间：
  - 同表并发写入被**串行化**，后到者在 READ COMMITTED 下看到已提交的最新前驱/后继，
    因此不漏段、不重叠；
  - 不同电表锁不同行，**互不阻塞**；
  - 固定加锁顺序使跨两只电表的改号事务**不会死锁**。
- 区间是**持久化增量维护**：每次写入只读取前驱、后继两条读数，改动至多三条区间行，
  走索引（见下文验证），不重扫整只电表；查询直接读区间表，不临时全量计算。
- 补算子段同样**持久化增量维护**：工单写入只重建换表时刻所在的至多一条缺测段；
  读数写入只重建改动时刻相邻的缺测段（改动前先快照受影响段，改动后去重重建）。
  `backfill_segments` 到区间/工单的外键为 **可延迟约束（DEFERRABLE INITIALLY DEFERRED）**
  并带级联，跨表改号在提交时统一校验，删段时旧补算随源数据同事务撤下。
- 写操作在单事务内完成，任一步失败**整体回滚**（工单/读数/区间/补算要么全部生效，要么全不生效）。
- `/supply`（及读接口）使用 **REPEATABLE READ** 单事务，读数、原始缺测、补算子段、工单
  四类数据取自同一个完整提交快照，并发或回滚下供数始终对应同一已提交状态。

## 6. 目录结构

```
sql/01_schema.sql          表结构、约束、电量纯函数 (含工单表、补算子段表)
sql/02_functions.sql       电表行锁 + 读数增量维护函数 (含补算刷新钩子) + 区间体检
sql/03_work_orders.sql     工单 UPSERT/删除 + 缺测段补算子段重建 + 补算体检
app/                       FastAPI 应用（连接池、模型、路由、错误映射）
scripts/migrate.py         幂等迁移
scripts/entrypoint.sh      容器入口（等库 -> 迁移 -> 启动）
scripts/concurrency_stress.py  真实并发压测（在容器/正常环境运行）
docs/examples/*.sql        补录 / 换表 / 并发 / 工单补算独立 SQL 示例
tests/                     pytest 自测（46 项）
docker-compose.yml / Dockerfile / requirements.txt
```

## 7. 自测

```bash
# 需要一个可连的 PostgreSQL（通过 DATABASE_URL 指定，库需已存在）
export DATABASE_URL=postgresql://postgres:postgres@localhost:55438/meterdb
python scripts/migrate.py            # 或由 compose 自动完成
PYTHONPATH=. pytest tests/
```

真实阻塞/唤醒并发压测（依赖 PostgreSQL 进程信号，需在容器或正常主机运行）：

```bash
docker compose exec api python scripts/concurrency_stress.py
```

一致性巡检（无行即完全一致，正常写入路径不依赖它、不做全表重算）：

```sql
-- 读数 -> 区间 的一致性
SELECT * FROM interval_consistency_report();
-- 缺测段 -> 工单 -> 持久化补算子段 的一致性
SELECT * FROM backfill_consistency_report();
```
