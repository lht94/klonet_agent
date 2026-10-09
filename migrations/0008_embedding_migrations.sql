-- ============================================================================ --
-- 0008: Embedding 模型迁移（04 计划阶段 6 / §6.5）
-- ============================================================================ --
-- 三件事：
--
-- 1. public.memory_embeddings —— 非 default profile 的向量存储。
--    ``memory_versions.embedding``（0001 的单列）保留给 default profile：
--    02 的读/写路径一字不动（零回归），新 profile 的向量落这张按
--    (memory_version_id, embedding_profile_id) 键控的表，实现"新旧并存"。
--    维度变化必须新建表（禁止 ALTER COLUMN vector(N)）——本表固定 1024 维，
--    未来换维度时由新迁移建新表，本表的 (version, profile) 键设计不变。
--
-- 2. memory_maintenance.embedding_migrations —— 迁移账本 + **落库的状态机**。
--    planned → backfilling → validating → ready → active → retiring → retired，
--    任意非终态可 failed / cancelled，failed 可回 backfilling（cursor 续跑）。
--    "没有显式创建迁移就不能写新 profile 向量"由应用层保证；"状态跳变"由
--    这里的 BEFORE UPDATE 触发器保证——不落库的状态机等于没写。
--
-- 3. memory_maintenance.embedding_active_profile —— 单例行，检索读它决定
--    用哪个 profile 的向量。原子切换 = 一条 UPDATE（同事务改迁移状态）。
--
-- 所有对象全部 schema 限定名（0006 的教训）。
-- ============================================================================ --

SET LOCAL search_path = memory_maintenance, public;

-- --------------------------------------------------------------------------- --
-- 1) 非 default profile 的向量存储
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS public.memory_embeddings (
    memory_version_id    uuid         NOT NULL
                                      REFERENCES public.memory_versions (id)
                                      ON DELETE CASCADE,
    embedding_profile_id text         NOT NULL
                                      CHECK (embedding_profile_id <> ''),
    -- 固定 1024 维（与 0001 一致）。换维度 = 新 profile + 新表，见文件头。
    embedding            vector(1024) NOT NULL,
    -- 身份列 NOT NULL：向量必须知道是哪个模型算的
    --（与 memory_versions_embedding_identified 同一纪律，但这里是硬 NOT NULL，
    --  因为这张表只为多 profile 并存而生，不存在"先有向量后补身份"的路径）。
    embedding_model      text         NOT NULL CHECK (embedding_model <> ''),
    embedding_version    text         NOT NULL CHECK (embedding_version <> ''),
    created_at           timestamptz  NOT NULL DEFAULT now(),
    updated_at           timestamptz  NOT NULL DEFAULT now(),
    -- 计划 §6.5："新旧 profile 向量并存，唯一键仍使用 (memory_version_id, profile_id)"。
    PRIMARY KEY (memory_version_id, embedding_profile_id)
);

CREATE INDEX IF NOT EXISTS memory_embeddings_profile_idx
    ON public.memory_embeddings (embedding_profile_id);

-- 同一 profile 下同一模型重复写入 = 覆盖（幂等），但**换模型**写同一 profile
-- 是事故（向量与身份不一致会造成召回污染）。模型身份对 (version, profile)
-- 不可变，换模型必须走新 profile + 新迁移。
CREATE OR REPLACE FUNCTION public.memory_embeddings_guard_identity()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.embedding_model IS DISTINCT FROM NEW.embedding_model
       OR OLD.embedding_version IS DISTINCT FROM NEW.embedding_version THEN
        RAISE EXCEPTION
            'memory_embeddings 的模型身份不可变（profile=%, version=%）：%/% -> %/%，换模型必须新建 profile 走迁移',
            OLD.embedding_profile_id, OLD.memory_version_id,
            OLD.embedding_model, OLD.embedding_version,
            NEW.embedding_model, NEW.embedding_version
        USING ERRCODE = '2F002';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS memory_embeddings_identity ON public.memory_embeddings;
CREATE TRIGGER memory_embeddings_identity
    BEFORE UPDATE ON public.memory_embeddings
    FOR EACH ROW EXECUTE FUNCTION public.memory_embeddings_guard_identity();

-- --------------------------------------------------------------------------- --
-- 2) 迁移账本 + 状态机
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS memory_maintenance.embedding_migrations (
    migration_id         text        PRIMARY KEY
                                     CHECK (migration_id <> ''),
    status               text        NOT NULL
                                     CHECK (status IN (
                                         'planned', 'backfilling', 'paused',
                                         'validating', 'ready', 'active',
                                         'retiring', 'retired',
                                         'failed', 'cancelled'
                                     )),
    source_profile_id    text        NOT NULL CHECK (source_profile_id <> ''),
    target_profile_id    text        NOT NULL CHECK (target_profile_id <> ''),
    -- 约束在表级 CHECK 里表达不了跨列，manager 会再校验；这里只挡明显笔误。
    target_model         text        NOT NULL CHECK (target_model <> ''),
    target_model_version text        NOT NULL CHECK (target_model_version <> ''),
    target_dimensions    integer     NOT NULL CHECK (target_dimensions > 0),
    -- 目标向量表名（public schema 内）。1024 维迁移填 'memory_embeddings'；
    -- 换维度的迁移填新表名，manager 在 create 时校验表存在且列维度匹配。
    target_table         text        NOT NULL CHECK (target_table <> ''),
    CHECK (target_profile_id <> source_profile_id),
    -- keyset cursor：(version_id)；形态由 manager 解释，这里只存 JSON。
    cursor               jsonb,
    -- validation 结果（coverage / recall / 延迟 / 成本）与推进原因。
    stats                jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    started_at           timestamptz,
    finished_at          timestamptz
);

-- 终态：retired / cancelled。
-- 同时只允许一个迁移处于非终态——两条迁移并发回填同一批版本会让 outbox
-- 与 cursor 语义互相踩踏；"先取消旧的再开新的"是刻意的摩擦。
CREATE UNIQUE INDEX IF NOT EXISTS embedding_migrations_one_live_idx
    ON memory_maintenance.embedding_migrations ((TRUE))
    WHERE status IN (
        'planned', 'backfilling', 'paused', 'validating', 'ready', 'active', 'retiring'
    );

CREATE INDEX IF NOT EXISTS embedding_migrations_status_idx
    ON memory_maintenance.embedding_migrations (status, updated_at DESC);

CREATE OR REPLACE FUNCTION memory_maintenance.touch_embedding_migrations_updated_at()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS embedding_migrations_touch
    ON memory_maintenance.embedding_migrations;
CREATE TRIGGER embedding_migrations_touch
    BEFORE UPDATE ON memory_maintenance.embedding_migrations
    FOR EACH ROW EXECUTE FUNCTION
        memory_maintenance.touch_embedding_migrations_updated_at();

-- 状态机（§6.5）：
--
--   planned     → backfilling | cancelled
--   backfilling → paused | validating | failed | cancelled
--   paused      → backfilling | validating | failed | cancelled
--   validating  → backfilling | ready | failed | cancelled
--   ready       → active | failed | cancelled
--   active      → retiring                    （切换后旧 profile 进入退役）
--   retiring    → retired
--   failed      → backfilling | cancelled     （失败重启从 cursor 续跑）
--   retired / cancelled 是终态
--
-- 注意 validating → backfilling 是合法的：validation 发现覆盖不达标时回炉
-- 继续补向量；ready → active 的门禁（coverage/eval 达标）在应用层校验，
-- 触发器只管"顺序合法"。
CREATE OR REPLACE FUNCTION memory_maintenance.embedding_migrations_guard_transition()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'planned'     AND NEW.status IN ('backfilling', 'cancelled') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'backfilling' AND NEW.status IN ('paused', 'validating', 'failed', 'cancelled') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'paused'      AND NEW.status IN ('backfilling', 'validating', 'failed', 'cancelled') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'validating'  AND NEW.status IN ('backfilling', 'ready', 'failed', 'cancelled') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'ready'       AND NEW.status IN ('active', 'failed', 'cancelled') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'active'      AND NEW.status = 'retiring' THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'retiring'    AND NEW.status = 'retired' THEN
        -- 进入终态时落 finished_at。
        NEW.finished_at := now();
        RETURN NEW;
    END IF;
    IF OLD.status = 'failed'      AND NEW.status IN ('backfilling', 'cancelled') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION
        'embedding 迁移状态转换非法：% -> %（migration_id=%）',
        OLD.status, NEW.status, OLD.migration_id
        USING ERRCODE = '2F002';
END
$$;

DROP TRIGGER IF EXISTS embedding_migrations_transition
    ON memory_maintenance.embedding_migrations;
CREATE TRIGGER embedding_migrations_transition
    BEFORE UPDATE OF status ON memory_maintenance.embedding_migrations
    FOR EACH ROW EXECUTE FUNCTION
        memory_maintenance.embedding_migrations_guard_transition();

-- --------------------------------------------------------------------------- --
-- 3) 当前生效 profile（单例）
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS memory_maintenance.embedding_active_profile (
    id          integer     PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    profile_id  text        NOT NULL CHECK (profile_id <> ''),
    model       text        NOT NULL CHECK (model <> ''),
    dimensions  integer     NOT NULL CHECK (dimensions > 0),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- default profile 的向量历史上写在 memory_versions.embedding，模型身份在
-- 各版本行上、并非全局唯一，所以这里落 'legacy' 占位——只有通过迁移门禁
-- 的 promote 才会原子改写这一行。
INSERT INTO memory_maintenance.embedding_active_profile
       (id, profile_id, model, dimensions)
VALUES (1, 'default', 'legacy', 1024)
ON CONFLICT (id) DO NOTHING;

-- --------------------------------------------------------------------------- --
-- 角色与授权（与 0006/0007 同一套：maint 写系统表、app/ops 读系统表；
-- public.memory_embeddings 是业务向量，与 memory_* 表同权限）
-- --------------------------------------------------------------------------- --

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_maint') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA memory_maintenance TO klonet_maint';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON
                     memory_maintenance.embedding_migrations,
                     memory_maintenance.embedding_active_profile
                 TO klonet_maint';
        EXECUTE 'GRANT EXECUTE ON FUNCTION
                     memory_maintenance.embedding_migrations_guard_transition(),
                     memory_maintenance.touch_embedding_migrations_updated_at()
                 TO klonet_maint';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON public.memory_embeddings TO klonet_maint';
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.memory_embeddings_guard_identity() TO klonet_maint';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_app') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA memory_maintenance TO klonet_app';
        EXECUTE 'GRANT SELECT ON
                     memory_maintenance.embedding_migrations,
                     memory_maintenance.embedding_active_profile
                 TO klonet_app';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON public.memory_embeddings TO klonet_app';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_ops') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA memory_maintenance TO klonet_ops';
        EXECUTE 'GRANT SELECT ON
                     memory_maintenance.embedding_migrations,
                     memory_maintenance.embedding_active_profile
                 TO klonet_ops';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON public.memory_embeddings TO klonet_ops';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_migrator') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA memory_maintenance TO klonet_migrator';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON
                     memory_maintenance.embedding_migrations,
                     memory_maintenance.embedding_active_profile
                 TO klonet_migrator';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON public.memory_embeddings TO klonet_migrator';
    END IF;
END
$$;
