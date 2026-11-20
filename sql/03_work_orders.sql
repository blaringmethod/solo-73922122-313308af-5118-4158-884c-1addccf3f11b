-- ============================================================================
-- 换表工单登记 + 缺测段补算子段的持久化增量维护
--
-- 语义 (需求):
--   * 工单按 (电表号, UTC 换表时刻) 唯一保存拆除读数/装机读数, 重复提交即更正;
--   * 仅当换表时刻 *严格* 落在某条换表缺测段 (reading_intervals.energy_wh IS NULL)
--     的两端之间时, 该工单才参与该段的补算;
--   * 对一条缺测段, 适用工单按换表时刻逐次切开: 拆除读数结束上一段,
--     装机读数开启下一段, 最外两端沿用原采样读数; 每段沿用单次回绕;
--   * 没有适用工单的缺测段保留缺测 (backfill_segments 中不产生行);
--   * 补算子段持久化在 backfill_segments; 读数/工单变化时只重建受影响段,
--     旧补算与源数据在同一事务内撤下 (外键级联 + 显式重建双保险)。
--
-- 并发: 所有写路径先经 lock_meters 持有电表行锁, 同表写操作串行化,
--       补算重建与读数/区间维护在同一事务内提交或回滚。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 收集时刻 p_read_at 所在(含边界相邻)的全部缺测段右端时刻。
-- 区间链首尾相接, 命中行至多 3 条, 走 (meter_no, end_read_at) 索引。
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS _gap_ends_near(TEXT, TIMESTAMPTZ);
CREATE OR REPLACE FUNCTION _gap_ends_near(
    p_meter_no TEXT,
    p_read_at  TIMESTAMPTZ
) RETURNS TABLE (gap_end_read_at TIMESTAMPTZ)
LANGUAGE sql AS $$
    SELECT i.end_read_at
      FROM reading_intervals i
     WHERE i.meter_no = p_meter_no
       AND i.energy_wh IS NULL
       AND i.start_read_at <= p_read_at
       AND i.end_read_at   >= p_read_at;
$$;

-- 改动区间前先快照受影响缺测段 (改动后旧段可能已被删除, 无法再查到)
DROP FUNCTION IF EXISTS _snapshot_gap_ends_near(TEXT, TIMESTAMPTZ);
CREATE OR REPLACE FUNCTION _snapshot_gap_ends_near(
    p_meter_no TEXT,
    p_read_at  TIMESTAMPTZ
) RETURNS TIMESTAMPTZ[]
LANGUAGE sql AS $$
    SELECT COALESCE(array_agg(gap_end_read_at), ARRAY[]::TIMESTAMPTZ[])
      FROM _gap_ends_near(p_meter_no, p_read_at);
$$;

-- ---------------------------------------------------------------------------
-- 重建一条缺测段的全部补算子段 (幂等: 先撤旧再按当前工单重算)。
-- 调用前须持有该电表的 meters 行锁。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE _rebuild_gap_backfill(
    p_meter_no   TEXT,
    p_gap_end    TIMESTAMPTZ
)
LANGUAGE plpgsql AS $$
DECLARE
    v_gap        reading_intervals%ROWTYPE;
    v_wo         meter_work_orders%ROWTYPE;
    v_cur_at     TIMESTAMPTZ;
    v_cur_read   INTEGER;
    v_cur_wo_at  TIMESTAMPTZ;
    v_idx        INTEGER := 0;
    v_wo_count   INTEGER;
BEGIN
    SELECT * INTO v_gap
      FROM reading_intervals
     WHERE meter_no = p_meter_no
       AND end_read_at = p_gap_end
    FOR UPDATE;

    -- 源缺测段已不存在: 外键 ON DELETE CASCADE 已把旧补算随源段同事务撤下
    IF NOT FOUND THEN
        RETURN;
    END IF;

    -- 无论后续走哪条分支, 先撤该段旧补算 (显式兜底, 与级联删除同一事务)
    DELETE FROM backfill_segments
     WHERE meter_no = p_meter_no
       AND gap_end_read_at = p_gap_end;

    -- 该段已不再缺测 (例如 replaced 标记被更正为 false): 不应保留任何补算
    IF v_gap.energy_wh IS NOT NULL THEN
        RETURN;
    END IF;

    -- 仅采用换表时刻严格落在缺测段两端之间的工单
    SELECT count(*) INTO v_wo_count
      FROM meter_work_orders w
     WHERE w.meter_no = p_meter_no
       AND w.replaced_at > v_gap.start_read_at
       AND w.replaced_at < v_gap.end_read_at;

    -- 没有工单 => 保留缺测, 不产生补算子段
    IF v_wo_count = 0 THEN
        RETURN;
    END IF;

    -- 最外左端: 原采样读数开启第一段
    v_cur_at    := v_gap.start_read_at;
    v_cur_read  := v_gap.start_reading;
    v_cur_wo_at := NULL;

    FOR v_wo IN
        SELECT *
          FROM meter_work_orders w
         WHERE w.meter_no = p_meter_no
           AND w.replaced_at > v_gap.start_read_at
           AND w.replaced_at < v_gap.end_read_at
         ORDER BY w.replaced_at
    LOOP
        -- 拆除读数结束当前段 (单次回绕; 段内不涉及换表标记, replaced=false)
        INSERT INTO backfill_segments (
            meter_no, gap_end_read_at, segment_index,
            start_read_at, end_read_at,
            start_reading, end_reading, energy_wh,
            start_work_order_at, end_work_order_at
        ) VALUES (
            p_meter_no, p_gap_end, v_idx,
            v_cur_at, v_wo.replaced_at,
            v_cur_read, v_wo.removal_reading,
            interval_energy(v_cur_read, v_wo.removal_reading, false),
            v_cur_wo_at, v_wo.replaced_at
        );
        v_idx := v_idx + 1;

        -- 装机读数开启下一段
        v_cur_at    := v_wo.replaced_at;
        v_cur_read  := v_wo.install_reading;
        v_cur_wo_at := v_wo.replaced_at;
    END LOOP;

    -- 最外右端: 原采样读数关闭最后一段
    INSERT INTO backfill_segments (
        meter_no, gap_end_read_at, segment_index,
        start_read_at, end_read_at,
        start_reading, end_reading, energy_wh,
        start_work_order_at, end_work_order_at
    ) VALUES (
        p_meter_no, p_gap_end, v_idx,
        v_cur_at, v_gap.end_read_at,
        v_cur_read, v_gap.end_reading,
        interval_energy(v_cur_read, v_gap.end_reading, false),
        v_cur_wo_at, NULL
    );
END;
$$;

-- ---------------------------------------------------------------------------
-- 读数写入后的统一钩子: 重建改动前快照中的缺测段 + 改动后时刻相邻的缺测段。
-- 去重后逐段重建; 已消失/不再缺测的段由重建过程自行跳过或清空。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE _refresh_gap_backfills_around(
    p_meter_no TEXT,
    p_read_at  TIMESTAMPTZ,
    p_before   TIMESTAMPTZ[]
)
LANGUAGE plpgsql AS $$
DECLARE
    v_end TIMESTAMPTZ;
BEGIN
    FOR v_end IN
        SELECT DISTINCT g.end_at
          FROM (
              SELECT unnest(p_before) AS end_at
               UNION
              SELECT gap_end_read_at FROM _gap_ends_near(p_meter_no, p_read_at)
          ) g
         WHERE g.end_at IS NOT NULL
    LOOP
        CALL _rebuild_gap_backfill(p_meter_no, v_end);
    END LOOP;
END;
$$;

-- ============================================================================
-- 工单登记 / 更正 / 删除
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 登记或更正工单 (同表同刻再次提交即更正), 只重建换表时刻所在的那一条
-- 缺测段 (严格包含); 不落任何缺测段内则只保存工单, 不动补算。
-- 返回列加 o_ 前缀, 避免与表列名在 PL/pgSQL 内歧义。
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS upsert_work_order(TEXT, TIMESTAMPTZ, INTEGER, INTEGER);
CREATE OR REPLACE FUNCTION upsert_work_order(
    p_meter_no        TEXT,
    p_replaced_at     TIMESTAMPTZ,
    p_removal_reading INTEGER,
    p_install_reading INTEGER
) RETURNS TABLE (
    o_id              BIGINT,
    o_meter_no        TEXT,
    o_replaced_at     TIMESTAMPTZ,
    o_removal_reading INTEGER,
    o_install_reading INTEGER,
    o_created_at      TIMESTAMPTZ,
    o_updated_at      TIMESTAMPTZ,
    o_created         BOOLEAN
)
LANGUAGE plpgsql AS $$
DECLARE
    v_created BOOLEAN;
    v_gap     reading_intervals%ROWTYPE;
BEGIN
    PERFORM lock_meters(p_meter_no);

    SELECT NOT EXISTS (
        SELECT 1 FROM meter_work_orders w
         WHERE w.meter_no = p_meter_no AND w.replaced_at = p_replaced_at
    ) INTO v_created;

    INSERT INTO meter_work_orders AS w (
        meter_no, replaced_at, removal_reading, install_reading
    ) VALUES (
        p_meter_no, p_replaced_at, p_removal_reading, p_install_reading
    )
    ON CONFLICT (meter_no, replaced_at) DO UPDATE
       SET removal_reading = EXCLUDED.removal_reading,
           install_reading = EXCLUDED.install_reading,
           updated_at      = now()
    RETURNING w.id, w.meter_no, w.replaced_at, w.removal_reading,
              w.install_reading, w.created_at, w.updated_at
        INTO o_id, o_meter_no, o_replaced_at, o_removal_reading,
             o_install_reading, o_created_at, o_updated_at;

    -- 换表时刻严格落在两端之间的缺测段至多一条 (区间链互不重叠)
    FOR v_gap IN
        SELECT i.*
          FROM reading_intervals i
         WHERE i.meter_no = p_meter_no
           AND i.energy_wh IS NULL
           AND i.start_read_at < p_replaced_at
           AND i.end_read_at   > p_replaced_at
    LOOP
        CALL _rebuild_gap_backfill(p_meter_no, v_gap.end_read_at);
    END LOOP;

    o_created := v_created;
    RETURN NEXT;
END;
$$;

-- ---------------------------------------------------------------------------
-- 删除工单: 受影响缺测段按剩余工单重建; 无剩余工单则回到缺测 (段行撤尽)。
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS delete_work_order(BIGINT);
CREATE OR REPLACE FUNCTION delete_work_order(p_id BIGINT)
RETURNS meter_work_orders
LANGUAGE plpgsql AS $$
DECLARE
    v_old     meter_work_orders%ROWTYPE;
    v_gap_end TIMESTAMPTZ;
BEGIN
    SELECT * INTO v_old FROM meter_work_orders w WHERE w.id = p_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION '工单不存在: id=%', p_id USING ERRCODE = 'P0002';
    END IF;

    PERFORM lock_meters(v_old.meter_no);

    SELECT * INTO v_old FROM meter_work_orders w WHERE w.id = p_id FOR UPDATE;

    -- 先定位受影响缺测段 (删除工单后仍可凭剩余工单重建)
    SELECT i.end_read_at INTO v_gap_end
      FROM reading_intervals i
     WHERE i.meter_no = v_old.meter_no
       AND i.energy_wh IS NULL
       AND i.start_read_at < v_old.replaced_at
       AND i.end_read_at   > v_old.replaced_at;

    DELETE FROM meter_work_orders WHERE id = p_id;

    IF v_gap_end IS NOT NULL THEN
        -- 无剩余适用工单时重建过程会清空补算, 缺测保留
        CALL _rebuild_gap_backfill(v_old.meter_no, v_gap_end);
    END IF;

    RETURN v_old;
END;
$$;

-- ============================================================================
-- 防御性触发器: 即使绕过函数直接对 meter_work_orders 做 DELETE/UPDATE
-- (API 与 upsert/delete 函数路径不受影响), 也在同一事务内重建受影响缺测段,
-- 保证"旧补算随源数据同事务撤下"对任意写入路径成立。
-- 语句级、按受影响换表时刻去重, 正常函数路径下至多空跑一次, 开销可忽略。
-- (PostgreSQL 不允许多事件触发器使用转换表, 故 DELETE/UPDATE 各建一个。)
-- ============================================================================
CREATE OR REPLACE FUNCTION _trg_work_orders_after_delete()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_meter   TEXT;
    v_at      TIMESTAMPTZ;
    v_gap_end TIMESTAMPTZ;
BEGIN
    FOR v_meter, v_at IN
        SELECT DISTINCT meter_no, replaced_at FROM old_table
    LOOP
        SELECT i.end_read_at INTO v_gap_end
          FROM reading_intervals i
         WHERE i.meter_no = v_meter
           AND i.energy_wh IS NULL
           AND i.start_read_at < v_at
           AND i.end_read_at   > v_at;
        IF v_gap_end IS NOT NULL THEN
            CALL _rebuild_gap_backfill(v_meter, v_gap_end);
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION _trg_work_orders_after_update()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_meter   TEXT;
    v_at      TIMESTAMPTZ;
    v_gap_end TIMESTAMPTZ;
BEGIN
    FOR v_meter, v_at IN
        SELECT meter_no, replaced_at FROM new_table
        UNION
        SELECT meter_no, replaced_at FROM old_table
    LOOP
        FOR v_gap_end IN
            SELECT gap_end_read_at FROM _gap_ends_near(v_meter, v_at)
        LOOP
            CALL _rebuild_gap_backfill(v_meter, v_gap_end);
        END LOOP;
    END LOOP;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_work_orders_after_delete ON meter_work_orders;
CREATE TRIGGER trg_work_orders_after_delete
    AFTER DELETE ON meter_work_orders
    REFERENCING OLD TABLE AS old_table
    FOR EACH STATEMENT
    EXECUTE FUNCTION _trg_work_orders_after_delete();

DROP TRIGGER IF EXISTS trg_work_orders_after_update ON meter_work_orders;
CREATE TRIGGER trg_work_orders_after_update
    AFTER UPDATE ON meter_work_orders
    REFERENCING OLD TABLE AS old_table NEW TABLE AS new_table
    FOR EACH STATEMENT
    EXECUTE FUNCTION _trg_work_orders_after_update();

-- ============================================================================
-- 补算一致性体检: 逐条缺测段核对持久化补算子段与当前工单/采样读数是否一致
-- 无行即完全一致 (巡检/测试用, 正常写入路径不依赖)
-- ============================================================================
DROP FUNCTION IF EXISTS backfill_consistency_report();
CREATE OR REPLACE FUNCTION backfill_consistency_report()
RETURNS TABLE (
    meter_no         TEXT,
    gap_end_read_at  TIMESTAMPTZ,
    problem          TEXT,
    detail           TEXT
)
LANGUAGE plpgsql AS $$
DECLARE
    v_gap        reading_intervals%ROWTYPE;
    v_wo         meter_work_orders%ROWTYPE;
    v_seg        backfill_segments%ROWTYPE;
    v_wo_count   INTEGER;
    v_seg_count  INTEGER;
    v_cur_at     TIMESTAMPTZ;
    v_cur_read   INTEGER;
    v_cur_wo_at  TIMESTAMPTZ;
    v_idx        INTEGER;
BEGIN
    -- 1) 孤立补算: 所属缺测段不存在或已不再缺测 (外键级联下理论上不会出现)
    RETURN QUERY
    SELECT s.meter_no, s.gap_end_read_at, 'orphan_backfill',
           '补算所属区间不存在或已不是缺测段'
      FROM backfill_segments s
      LEFT JOIN reading_intervals i
        ON i.meter_no = s.meter_no
       AND i.end_read_at = s.gap_end_read_at
     WHERE i.meter_no IS NULL OR i.energy_wh IS NOT NULL;

    -- 2) 逐条缺测段核对
    FOR v_gap IN
        SELECT i.* FROM reading_intervals i
         WHERE i.energy_wh IS NULL
         ORDER BY i.meter_no, i.end_read_at
    LOOP
        SELECT count(*) INTO v_wo_count
          FROM meter_work_orders w
         WHERE w.meter_no = v_gap.meter_no
           AND w.replaced_at > v_gap.start_read_at
           AND w.replaced_at < v_gap.end_read_at;

        SELECT count(*) INTO v_seg_count
          FROM backfill_segments s
         WHERE s.meter_no = v_gap.meter_no
           AND s.gap_end_read_at = v_gap.end_read_at;

        IF v_wo_count = 0 THEN
            IF v_seg_count <> 0 THEN
                RETURN QUERY SELECT v_gap.meter_no, v_gap.end_read_at,
                    'unexpected_backfill', '无适用工单却存在补算子段';
            END IF;
            CONTINUE;
        END IF;

        IF v_seg_count <> v_wo_count + 1 THEN
            RETURN QUERY SELECT v_gap.meter_no, v_gap.end_read_at,
                'segment_count_mismatch',
                format('工单数 %s, 期望子段 %s, 实际 %s',
                       v_wo_count, v_wo_count + 1, v_seg_count);
            CONTINUE;
        END IF;

        -- 按重建同样的状态机逐段比对端点/读数/电量/工单时刻
        v_cur_at    := v_gap.start_read_at;
        v_cur_read  := v_gap.start_reading;
        v_cur_wo_at := NULL;
        v_idx       := 0;

        FOR v_wo IN
            SELECT * FROM meter_work_orders w
             WHERE w.meter_no = v_gap.meter_no
               AND w.replaced_at > v_gap.start_read_at
               AND w.replaced_at < v_gap.end_read_at
             ORDER BY w.replaced_at
        LOOP
            SELECT * INTO v_seg FROM backfill_segments s
             WHERE s.meter_no = v_gap.meter_no
               AND s.gap_end_read_at = v_gap.end_read_at
               AND s.segment_index = v_idx;

            IF v_seg.meter_no IS NULL
               OR v_seg.start_read_at IS DISTINCT FROM v_cur_at
               OR v_seg.end_read_at   IS DISTINCT FROM v_wo.replaced_at
               OR v_seg.start_reading IS DISTINCT FROM v_cur_read
               OR v_seg.end_reading   IS DISTINCT FROM v_wo.removal_reading
               OR v_seg.energy_wh     IS DISTINCT FROM
                    interval_energy(v_cur_read, v_wo.removal_reading, false)
               OR v_seg.start_work_order_at IS DISTINCT FROM v_cur_wo_at
               OR v_seg.end_work_order_at   IS DISTINCT FROM v_wo.replaced_at
            THEN
                RETURN QUERY SELECT v_gap.meter_no, v_gap.end_read_at,
                    'wrong_segment', format('子段 %s 与工单/读数推导不一致', v_idx);
                CONTINUE;
            END IF;

            v_cur_at    := v_wo.replaced_at;
            v_cur_read  := v_wo.install_reading;
            v_cur_wo_at := v_wo.replaced_at;
            v_idx       := v_idx + 1;
        END LOOP;

        -- 末段: 右端为原采样读数
        SELECT * INTO v_seg FROM backfill_segments s
         WHERE s.meter_no = v_gap.meter_no
           AND s.gap_end_read_at = v_gap.end_read_at
           AND s.segment_index = v_idx;

        IF v_seg.meter_no IS NULL
           OR v_seg.start_read_at IS DISTINCT FROM v_cur_at
           OR v_seg.end_read_at   IS DISTINCT FROM v_gap.end_read_at
           OR v_seg.start_reading IS DISTINCT FROM v_cur_read
           OR v_seg.end_reading   IS DISTINCT FROM v_gap.end_reading
           OR v_seg.energy_wh     IS DISTINCT FROM
                interval_energy(v_cur_read, v_gap.end_reading, false)
           OR v_seg.start_work_order_at IS DISTINCT FROM v_cur_wo_at
           OR v_seg.end_work_order_at   IS NOT NULL
        THEN
            RETURN QUERY SELECT v_gap.meter_no, v_gap.end_read_at,
                'wrong_segment', format('末段(子段 %s)与采样读数不一致', v_idx);
        END IF;
    END LOOP;
END;
$$;
