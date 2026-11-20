-- ============================================================================
-- 示例 4: 换表工单登记 + 缺测段补算子段 (负荷分析分开看换表前后用电)
--
-- 场景: 电表 M-004 在 01:00->02:00 之间换表, 02:00 是新表首读 (replaced=true),
--       因此区间 (01:00,02:00] 电量缺测 (NULL)。事后拿到换表工单:
--         换表时刻 01:30, 拆除读数(旧表末读) 999800, 装机读数(新表首读) 100。
--
-- 规则:
--   * 只采用换表时刻 *严格* 落在缺测段两端之间的工单 (等于端点不参与);
--   * 拆除读数结束上一段, 装机读数开启下一段, 最外两端沿用原采样读数;
--   * 每个子段沿用 1,000,000 单次回绕;
--   * 没有工单的缺测段保留缺测; 补算子段持久化在 backfill_segments;
--   * 同表同刻再次提交 upsert_work_order 即更正, 旧补算同事务重建。
--
-- 也可直接用 API (见 README):
--   POST /work-orders  登记/更正;  GET /supply  分开返回原始缺测与补算子段。
-- ============================================================================

BEGIN;

-- 1) 采样读数: (01:00,02:00] 为换表缺测段
SELECT insert_reading('M-004', '2026-01-01 00:00:00+00',  1000, false);
SELECT insert_reading('M-004', '2026-01-01 01:00:00+00', 999500, false);
SELECT insert_reading('M-004', '2026-01-01 02:00:00+00',   200, true);  -- 新表首读
SELECT insert_reading('M-004', '2026-01-01 03:00:00+00',   800, false);

-- 登记前: 缺测段 energy_wh 为 NULL, 且没有任何补算子段
SELECT meter_no, start_read_at, end_read_at, energy_wh
  FROM reading_intervals
 WHERE meter_no = 'M-004'
 ORDER BY end_read_at;
-- (01:00,02:00] = NULL

SELECT count(*) AS backfill_segments_before
  FROM backfill_segments
 WHERE meter_no = 'M-004';
-- 0

-- 2) 登记换表工单 (同表同刻再提交就是更正, o_created 由 t 变 f)
SELECT o_id, o_created AS first_register
  FROM upsert_work_order(
        'M-004',
        '2026-01-01 01:30:00+00',  -- UTC 换表时刻 (严格在 01:00 与 02:00 之间)
        999800,                    -- 拆除读数 (旧表末读)
        100                        -- 装机读数 (新表首读)
       );

-- 3) 缺测段被逐次切成两个持久化补算子段
SELECT segment_index,
       start_read_at, end_read_at,
       start_reading, end_reading, energy_wh,
       start_work_order_at, end_work_order_at
  FROM backfill_segments
 WHERE meter_no = 'M-004'
 ORDER BY segment_index;
-- 期望:
--   0  (01:00,01:30]  999500 -> 999800 = 300 Wh  左端采样(NULL工单) 右端工单
--   1  (01:30,02:00]  100    -> 200    = 100 Wh  左端工单 右端采样(NULL工单)

-- 原始缺测值仍保留在 reading_intervals (energy_wh = NULL), 与补算分开:
SELECT start_read_at, end_read_at, energy_wh AS raw_missing
  FROM reading_intervals
 WHERE meter_no = 'M-004' AND energy_wh IS NULL;

-- 4) 同表同刻更正工单 -> 旧补算同事务撤下并按新读数重建
SELECT o_id, o_created AS is_correction
  FROM upsert_work_order(
        'M-004', '2026-01-01 01:30:00+00', 999900, 50
       );
-- is_correction = false, o_id 与首次登记相同

SELECT segment_index, start_reading, end_reading, energy_wh
  FROM backfill_segments
 WHERE meter_no = 'M-004'
 ORDER BY segment_index;
-- 期望: 400 / 150

-- 5) 边界严格性: 换表时刻恰好等于缺测段端点的工单不参与补算 (只登记)
SELECT o_id FROM upsert_work_order(
        'M-004', '2026-01-01 01:00:00+00', 999600, 10);  -- 等于左端 => 不切段
SELECT count(*) AS still_two_segments
  FROM backfill_segments WHERE meter_no = 'M-004';      -- 仍是 2 段

-- 6) 巡检: 无行即补算子段与当前工单/采样读数完全一致
SELECT * FROM backfill_consistency_report() WHERE meter_no = 'M-004';

-- 7) 删除工单: 无剩余适用工单 => 补算子段撤尽, 缺测保留
SELECT delete_work_order(
        (SELECT id FROM meter_work_orders
          WHERE meter_no = 'M-004' AND replaced_at = '2026-01-01 01:30:00+00')
       );
SELECT count(*) AS backfill_after_delete
  FROM backfill_segments WHERE meter_no = 'M-004';      -- 0 (缺测保留)

ROLLBACK;  -- 演示库可重复执行; 确认无误后可改 COMMIT
