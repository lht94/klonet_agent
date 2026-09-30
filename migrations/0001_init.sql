-- 0001_init.sql — 记忆系统核心 schema
--
-- 对应 `通用功能升级计划/02-记忆系统升级计划.md` §5.2（六张核心表）与阶段 1 清单。
--
-- 执行方式（本文件不含 BEGIN/COMMIT，事务由调用方负责）：
--   * 迁移执行器：python -c "from klonet_agent.memory.database import MemoryDatabase;
--                   db = MemoryDatabase.from_env(); db.run_migrations()"
--   * 手工执行：  psql --single-transaction -f migrations/0001_init.sql
--   迁移执行器会把整个文件放进一个事务里，失败则整体回滚，不留半套 schema。
--
-- 幂等性：全部使用 IF NOT EXISTS / DO 块判存，重复执行无副作用。
--
-- 维度说明：`memory_versions.embedding` 固定为 vector(1024)，对应
-- `DEFAULT_EMBEDDING_MODEL=text-embedding-v4` 的默认维度（见 config.py）。
-- §7.3 描述的「多 embedding profile 并存、换维度不原地改」属于阶段 4：
-- 届时新建 `memory_embedding_profiles` + `memory_embeddings`，回填后删除本列，
-- 本列在阶段 1~3 只承载「唯一 active profile」。

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

-- --------------------------------------------------------------------------- --
-- memory_records：逻辑记忆的当前身份，不保存可变正文
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS memory_records (
    id                uuid        PRIMARY KEY,
    user_id           text        NOT NULL CHECK (user_id <> ''),
    project_id        text,
    scope             text        NOT NULL
                                  CHECK (scope IN ('user', 'project', 'shared_ops')),
    memory_type       text        NOT NULL
                                  CHECK (memory_type IN ('episode', 'fact', 'preference')),
    subject_key       text        NOT NULL CHECK (subject_key <> ''),
    status            text        NOT NULL DEFAULT 'active'
                                  CHECK (status IN ('active', 'superseded', 'expired', 'deleted')),
    active_version_id uuid,
    importance        real        NOT NULL DEFAULT 0.5
                                  CHECK (importance >= 0 AND importance <= 1),
    confidence        real        NOT NULL DEFAULT 0.5
                                  CHECK (confidence >= 0 AND confidence <= 1),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    -- 与 memory/domain.py 的 MemoryRecord 校验保持一致：
    -- 项目作用域必须有 project_id，其他作用域必须没有。
    CONSTRAINT memory_records_project_scope_consistent
        CHECK ((scope = 'project') = (project_id IS NOT NULL)),
    -- project_id 只允许非空串或 NULL。
    -- RLS 策略里用 `project_id = current_setting('app.project_id', true)` 比较，
    -- 而租户变量在事务结束后会**退化成空串**（PostgreSQL 自定义 GUC 的占位符
    -- 行为：一旦设置过，revert 后 current_setting 返回 '' 而不是 NULL）。
    -- 如果库里存在 project_id = '' 的行，未绑定租户的会话就会意外匹配到它。
    CONSTRAINT memory_records_project_id_not_empty
        CHECK (project_id IS NULL OR project_id <> '')
);

-- --------------------------------------------------------------------------- --
-- memory_versions：不可变版本。正文、有效时间、embedding 都挂在这里
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS memory_versions (
    id                uuid        PRIMARY KEY,
    memory_id         uuid        NOT NULL
                                  REFERENCES memory_records (id) ON DELETE CASCADE,
    version           integer     NOT NULL CHECK (version > 0),
    content           text        NOT NULL CHECK (content <> ''),
    summary           text,
    metadata          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- jieba 分词后的规范 token（空格分隔），阶段 1 先承载中文全文检索。
    lexical_text      text        NOT NULL DEFAULT '',
    -- to_tsvector(regconfig, text) 是 IMMUTABLE，可以作为 STORED 生成列；
    -- 中文词典交给 Python 侧 jieba，这里固定用 simple，只做 token 精确匹配。
    lexical_tsv       tsvector    GENERATED ALWAYS AS (
                                      to_tsvector('simple'::regconfig, COALESCE(lexical_text, ''))
                                  ) STORED,
    embedding         vector(1024),
    embedding_model   text,
    embedding_version text,
    content_hash      text        NOT NULL CHECK (content_hash <> ''),
    observed_at       timestamptz NOT NULL,
    valid_from        timestamptz NOT NULL,
    valid_to          timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT memory_versions_validity_ordered
        CHECK (valid_to IS NULL OR valid_to > valid_from),
    -- embedding 与它的身份必须同生同灭，避免出现"有向量但不知道是哪个模型算的"。
    CONSTRAINT memory_versions_embedding_identified
        CHECK (
            embedding IS NULL
            OR (embedding_model IS NOT NULL AND embedding_version IS NOT NULL)
        ),
    -- 幂等：(memory_id, version) 防重复版本号；(memory_id, content_hash) 防内容等价重复写入。
    CONSTRAINT memory_versions_unique_number UNIQUE (memory_id, version),
    CONSTRAINT memory_versions_unique_content UNIQUE (memory_id, content_hash)
);

-- 逻辑记录指向当前版本。放在两张表都建好之后，打破循环外键。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'memory_records_active_version_fk'
    ) THEN
        ALTER TABLE memory_records
            ADD CONSTRAINT memory_records_active_version_fk
            FOREIGN KEY (active_version_id) REFERENCES memory_versions (id)
            ON DELETE SET NULL;
    END IF;
END $$;

-- --------------------------------------------------------------------------- --
-- memory_sources：把结论关联回原始证据
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS memory_sources (
    id                bigserial   PRIMARY KEY,
    memory_version_id uuid        NOT NULL
                                  REFERENCES memory_versions (id) ON DELETE CASCADE,
    source_type       text        NOT NULL
                                  CHECK (source_type IN (
                                      'history_event', 'tool_result', 'journal', 'user_statement'
                                  )),
    source_id         text        NOT NULL CHECK (source_id <> ''),
    -- 摘录必须脱敏并限长，完整证据留在原事件系统里。
    -- 1000 与 memory/domain.py 的 SOURCE_EXCERPT_MAX_CHARS 一致。
    source_excerpt    text        NOT NULL DEFAULT ''
                                  CHECK (char_length(source_excerpt) <= 1000),
    observed_at       timestamptz NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    -- 同一版本追加同一来源必须幂等，不产生重复证据行。
    CONSTRAINT memory_sources_unique_evidence
        UNIQUE (memory_version_id, source_type, source_id)
);

-- --------------------------------------------------------------------------- --
-- memory_relations：首版只保存必要关系，不上图数据库
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS memory_relations (
    from_memory_id uuid        NOT NULL REFERENCES memory_records (id) ON DELETE CASCADE,
    relation_type  text        NOT NULL
                               CHECK (relation_type IN (
                                   'supersedes', 'supports', 'contradicts', 'related_to'
                               )),
    to_memory_id   uuid        NOT NULL REFERENCES memory_records (id) ON DELETE CASCADE,
    confidence     real        NOT NULL DEFAULT 0.5
                               CHECK (confidence >= 0 AND confidence <= 1),
    -- 计划要求 supersede 必须可审计；原因落在这里，而不是塞进版本的 metadata。
    reason         text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (from_memory_id, relation_type, to_memory_id),
    CONSTRAINT memory_relations_no_self_reference
        CHECK (from_memory_id <> to_memory_id)
);

-- --------------------------------------------------------------------------- --
-- memory_write_candidates：待处理候选及决策结果（审计视图）
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS memory_write_candidates (
    id                 uuid        PRIMARY KEY,
    -- 幂等键：同一事件区间重复提取候选不得产生第二行。
    idempotency_key    text        NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    user_id            text        NOT NULL CHECK (user_id <> ''),
    project_id         text,
    source_event_range jsonb       NOT NULL DEFAULT '{}'::jsonb,
    candidate_payload  jsonb       NOT NULL,
    decision           text        CHECK (decision IN ('add', 'update', 'supersede', 'noop', 'reject')),
    decision_reason    text,
    processed_at       timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    -- 决策与处理时间必须同时存在或同时缺失，避免"已决策但不知道何时"。
    CONSTRAINT memory_write_candidates_decision_complete
        CHECK ((decision IS NULL) = (processed_at IS NULL))
);

-- --------------------------------------------------------------------------- --
-- memory_embedding_outbox：正文事务与 embedding 异步生成解耦
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS memory_embedding_outbox (
    memory_version_id    uuid        NOT NULL
                                     REFERENCES memory_versions (id) ON DELETE CASCADE,
    -- 复合主键为阶段 4 的多 profile 并存预留，阶段 1 只写 'default'。
    embedding_profile_id text        NOT NULL DEFAULT 'default'
                                     CHECK (embedding_profile_id <> ''),
    status               text        NOT NULL DEFAULT 'pending'
                                     CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    attempt_count        integer     NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error           text,
    next_attempt_at      timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_version_id, embedding_profile_id)
);

-- --------------------------------------------------------------------------- --
-- 索引
-- --------------------------------------------------------------------------- --

-- 阶段 1 清单要求的四元组索引，覆盖最常见的作用域过滤。
CREATE INDEX IF NOT EXISTS idx_memory_records_scope
    ON memory_records (user_id, project_id, status, memory_type);

-- 同一 (user, project, scope, type, subject_key) 只能有一条 active 记忆。
-- 这条唯一索引是 ActiveSubjectConflictError 的数据库级保证：
-- 应用层忘记检查也插不进第二条"当前有效"的同一事实。
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_records_active_subject
    ON memory_records (user_id, COALESCE(project_id, ''), scope, memory_type, subject_key)
    WHERE status = 'active';

-- 全文 GIN 索引。
CREATE INDEX IF NOT EXISTS idx_memory_versions_lexical
    ON memory_versions USING gin (lexical_tsv);

-- 按记忆取版本时间线、以及 as_of 时态查询。
CREATE INDEX IF NOT EXISTS idx_memory_versions_validity
    ON memory_versions (memory_id, valid_from, valid_to);

CREATE INDEX IF NOT EXISTS idx_memory_relations_to
    ON memory_relations (to_memory_id, relation_type);

-- 待处理候选队列。
CREATE INDEX IF NOT EXISTS idx_memory_candidates_pending
    ON memory_write_candidates (created_at)
    WHERE decision IS NULL;

-- embedding worker 的取任务队列。
CREATE INDEX IF NOT EXISTS idx_memory_outbox_pending
    ON memory_embedding_outbox (status, next_attempt_at)
    WHERE status <> 'completed';

-- 阶段 1 只做 exact vector search（无 ANN 索引）。达到实测规模后再按计划
-- §12 的结论评估 HNSW：多了多租户过滤，先测量过滤后候选量再建索引。

-- --------------------------------------------------------------------------- --
-- Row-Level Security
-- --------------------------------------------------------------------------- --
--
-- 租户变量由应用在每个事务开始时用 SET LOCAL 写入（见 memory/database.py）：
--   app.user_id / app.project_id
-- 事务结束自动清除，不残留在连接池会话里。
--
-- 未绑定租户的连接：current_setting(..., true) 返回 NULL，比较结果为 NULL，
-- 因此看不到任何行——"忘记绑定"的失败方向是安全的（读不到，而不是读到别人的）。
--
-- shared_ops **被这条策略显式排除**，只能靠 0002 里按角色放行的
-- memory_records_shared_ops 读到。否则只要共享记忆的 user_id 恰好等于某个
-- 普通租户的 id，那个租户就能读到全部共享运维记忆。
--
-- 放行也不用 GUC：会话自己能 set_config，用可自设的变量当权限等于没有权限。

ALTER TABLE memory_records          ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_versions         ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_sources          ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_relations        ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_write_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_embedding_outbox ENABLE ROW LEVEL SECURITY;

-- 表 owner 默认绕过 RLS。生产上应用角色不是 owner（见 0002），但为了防住
-- "owner 角色被误用于在线请求"这种情况，显式强制。代价是 owner 需要对用户数据
-- 做批量维护时必须显式绑定租户或使用带 BYPASSRLS 的独立维护角色。
ALTER TABLE memory_records          FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_versions         FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_sources          FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_relations        FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_write_candidates FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_embedding_outbox FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS memory_records_tenant ON memory_records;
CREATE POLICY memory_records_tenant ON memory_records
    FOR ALL
    USING (
        scope <> 'shared_ops'
        AND user_id = current_setting('app.user_id', true)
        AND (
            scope <> 'project'
            OR project_id = current_setting('app.project_id', true)
        )
    )
    WITH CHECK (
        scope <> 'shared_ops'
        AND user_id = current_setting('app.user_id', true)
        AND (
            scope <> 'project'
            OR project_id = current_setting('app.project_id', true)
        )
    );

-- 候选项没有 scope 列，无法像记忆那样用 shared_ops 放行：
-- 放行一次就意味着一个 Ops 会话能读到全部用户的候选正文，太宽。
-- 因此候选项严格按 user_id 隔离（project_id 只是附加约束，不作为放行条件）。
DROP POLICY IF EXISTS memory_write_candidates_tenant ON memory_write_candidates;
CREATE POLICY memory_write_candidates_tenant ON memory_write_candidates
    FOR ALL
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));

-- 子表本身不带 user_id，通过 EXISTS 沿外键回溯到 memory_records。
-- 子查询里 memory_records 的 policy 同样生效，所以这里无需重复写租户条件：
-- 只要那条逻辑记忆对当前租户不可见，它的版本/来源/关系/任务就都不可见。
DROP POLICY IF EXISTS memory_versions_tenant ON memory_versions;
CREATE POLICY memory_versions_tenant ON memory_versions
    FOR ALL
    USING (EXISTS (
        SELECT 1 FROM memory_records r WHERE r.id = memory_versions.memory_id
    ))
    WITH CHECK (EXISTS (
        SELECT 1 FROM memory_records r WHERE r.id = memory_versions.memory_id
    ));

DROP POLICY IF EXISTS memory_sources_tenant ON memory_sources;
CREATE POLICY memory_sources_tenant ON memory_sources
    FOR ALL
    USING (EXISTS (
        SELECT 1 FROM memory_versions v
        JOIN memory_records r ON r.id = v.memory_id
        WHERE v.id = memory_sources.memory_version_id
    ))
    WITH CHECK (EXISTS (
        SELECT 1 FROM memory_versions v
        JOIN memory_records r ON r.id = v.memory_id
        WHERE v.id = memory_sources.memory_version_id
    ));

DROP POLICY IF EXISTS memory_relations_tenant ON memory_relations;
CREATE POLICY memory_relations_tenant ON memory_relations
    FOR ALL
    USING (
        EXISTS (SELECT 1 FROM memory_records r WHERE r.id = memory_relations.from_memory_id)
        AND EXISTS (SELECT 1 FROM memory_records r WHERE r.id = memory_relations.to_memory_id)
    )
    WITH CHECK (
        EXISTS (SELECT 1 FROM memory_records r WHERE r.id = memory_relations.from_memory_id)
        AND EXISTS (SELECT 1 FROM memory_records r WHERE r.id = memory_relations.to_memory_id)
    );

DROP POLICY IF EXISTS memory_embedding_outbox_tenant ON memory_embedding_outbox;
CREATE POLICY memory_embedding_outbox_tenant ON memory_embedding_outbox
    FOR ALL
    USING (EXISTS (
        SELECT 1 FROM memory_versions v
        JOIN memory_records r ON r.id = v.memory_id
        WHERE v.id = memory_embedding_outbox.memory_version_id
    ))
    WITH CHECK (EXISTS (
        SELECT 1 FROM memory_versions v
        JOIN memory_records r ON r.id = v.memory_id
        WHERE v.id = memory_embedding_outbox.memory_version_id
    ));
