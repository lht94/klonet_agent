-- 0005_governance_provenance.sql — 证据与 provenance（03 计划阶段 4）+ 路由决策（阶段 5 影子）
--
-- 对应计划 §4.3（证据管理）与 §4.5（模型路由决策落治理层）：
--   governance.evidence          通用 EvidenceRecord：来源、观察、时效、哈希、派生
--   governance.claims            主张（系统/用户提出的结论），与证据分离
--   governance.claim_evidence    主张-证据多对多（supports / contradicts / uncertain）
--   governance.route_decisions   模型路由决策（shadow 阶段：策略选择 vs 实际模型）
--
-- 设计要点（沿用 0004 的约定）：
--   * 与记忆系统共用租户变量 app.user_id / app.project_id 的 RLS；
--   * 事件与投影同事务由应用层保证；本迁移的外键全部 DEFERRABLE，
--     允许"先写引用它的事件、同事务再写被引用行"；
--   * evidence 不存大 payload：只存观察预览与内容寻址哈希，原文在
--     工具输出/文件里，哈希用于 stale 检测；
--   * 冲突证据不覆盖：应用层检测到同 subject 冲突时创建
--     claims.status='contradicted' 并把两条证据都挂上（claim_evidence）。

CREATE TABLE IF NOT EXISTS governance.evidence (
    evidence_id         text        PRIMARY KEY,
    run_id              text        NOT NULL REFERENCES governance.runs (run_id),
    user_id             text        NOT NULL CHECK (user_id <> ''),
    project_id          text,
    source_type         text        NOT NULL CHECK (source_type IN
                                        ('tool', 'file', 'command', 'http',
                                         'database', 'user_statement',
                                         'memory_recall', 'ops_probe')),
    source_uri          text        NOT NULL CHECK (source_uri <> ''),
    source_revision     text,
    subject             text        NOT NULL CHECK (subject <> ''),
    observation         text        NOT NULL DEFAULT '',
    observed_at         timestamptz NOT NULL,
    valid_from          timestamptz NOT NULL,
    valid_until         timestamptz,
    scope               text        NOT NULL DEFAULT 'project'
                                    CHECK (scope IN ('user', 'project', 'session')),
    confidence          real        NOT NULL DEFAULT 0.5
                                    CHECK (confidence >= 0 AND confidence <= 1),
    freshness           text        NOT NULL DEFAULT 'fresh'
                                    CHECK (freshness IN ('fresh', 'stale', 'unknown')),
    artifact_hash       text        CHECK (artifact_hash IS NULL OR artifact_hash <> ''),
    producer            text,
    parent_evidence_ids jsonb       NOT NULL DEFAULT '[]'::jsonb,
    privacy_class       text        NOT NULL DEFAULT 'internal'
                                    CHECK (privacy_class IN
                                        ('public', 'internal', 'sensitive', 'secret')),
    idempotency_key     text        UNIQUE,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT evidence_project_id_not_empty
        CHECK (project_id IS NULL OR project_id <> '')
);

CREATE INDEX IF NOT EXISTS evidence_subject_idx
    ON governance.evidence (user_id, project_id, subject);
CREATE INDEX IF NOT EXISTS evidence_run_idx
    ON governance.evidence (run_id, observed_at);
CREATE INDEX IF NOT EXISTS evidence_hash_idx
    ON governance.evidence (artifact_hash);

CREATE TABLE IF NOT EXISTS governance.claims (
    claim_id        text        PRIMARY KEY,
    run_id          text        NOT NULL REFERENCES governance.runs (run_id),
    user_id         text        NOT NULL CHECK (user_id <> ''),
    project_id      text,
    subject         text        NOT NULL CHECK (subject <> ''),
    statement       text        NOT NULL CHECK (statement <> ''),
    status          text        NOT NULL DEFAULT 'uncertain'
                                CHECK (status IN ('supported', 'contradicted', 'uncertain')),
    confidence      real        NOT NULL DEFAULT 0.5
                                CHECK (confidence >= 0 AND confidence <= 1),
    task_id         text        REFERENCES governance.tasks (task_id)
                                DEFERRABLE INITIALLY DEFERRED,
    turn_id         text        REFERENCES governance.turns (turn_id)
                                DEFERRABLE INITIALLY DEFERRED,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT claims_project_id_not_empty
        CHECK (project_id IS NULL OR project_id <> '')
);

CREATE INDEX IF NOT EXISTS claims_run_idx
    ON governance.claims (run_id, status);

CREATE TABLE IF NOT EXISTS governance.claim_evidence (
    id              bigserial   PRIMARY KEY,
    claim_id        text        NOT NULL REFERENCES governance.claims (claim_id)
                                DEFERRABLE INITIALLY DEFERRED,
    evidence_id     text        NOT NULL REFERENCES governance.evidence (evidence_id)
                                DEFERRABLE INITIALLY DEFERRED,
    user_id         text        NOT NULL CHECK (user_id <> ''),
    project_id      text,
    relation        text        NOT NULL CHECK (relation IN ('supports', 'contradicts', 'uncertain')),
    weight          real        NOT NULL DEFAULT 1.0 CHECK (weight > 0),
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT claim_evidence_unique UNIQUE (claim_id, evidence_id, relation),
    CONSTRAINT claim_evidence_project_id_not_empty
        CHECK (project_id IS NULL OR project_id <> '')
);

CREATE INDEX IF NOT EXISTS claim_evidence_claim_idx
    ON governance.claim_evidence (claim_id);
CREATE INDEX IF NOT EXISTS claim_evidence_evidence_idx
    ON governance.claim_evidence (evidence_id);

CREATE TABLE IF NOT EXISTS governance.route_decisions (
    decision_id     text        PRIMARY KEY,
    run_id          text        NOT NULL REFERENCES governance.runs (run_id),
    user_id         text        NOT NULL CHECK (user_id <> ''),
    project_id      text,
    task_level      text        NOT NULL,
    selected        text        NOT NULL CHECK (selected <> ''),
    actual_model    text,
    shadow          boolean     NOT NULL DEFAULT true,
    policy_version  text        NOT NULL,
    reason_codes    jsonb       NOT NULL DEFAULT '[]'::jsonb,
    candidate_scores jsonb      NOT NULL DEFAULT '{}'::jsonb,
    excluded        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    occurred_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS route_decisions_run_idx
    ON governance.route_decisions (run_id, occurred_at);

-- --------------------------------------------------------------------------- --
-- RLS：与 0004 同一套租户策略
-- --------------------------------------------------------------------------- --

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'evidence', 'claims', 'claim_evidence', 'route_decisions'
    ] LOOP
        EXECUTE format(
            'ALTER TABLE governance.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'ALTER TABLE governance.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('DROP POLICY IF EXISTS governance_tenant ON governance.%I', table_name);
        EXECUTE format($policy$
            CREATE POLICY governance_tenant ON governance.%I
                FOR ALL
                USING (
                    user_id = current_setting('app.user_id', true)
                    AND (
                        project_id IS NULL
                        OR project_id = current_setting('app.project_id', true)
                    )
                )
                WITH CHECK (
                    user_id = current_setting('app.user_id', true)
                    AND (
                        project_id IS NULL
                        OR project_id = current_setting('app.project_id', true)
                    )
                )
        $policy$, table_name);
    END LOOP;
END
$$;

-- 授权：角色沿用 0002；缺角色时告警不中断（fail-closed，不放开权限）。
DO $$
DECLARE
    role_exists boolean;
BEGIN
    SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_app') INTO role_exists;
    IF role_exists THEN
        EXECUTE 'GRANT USAGE ON SCHEMA governance TO klonet_app';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA governance TO klonet_app';
        EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA governance TO klonet_app';
    ELSE
        RAISE WARNING '[0005] klonet_app 不存在，governance 授权未创建（fail-closed）';
    END IF;

    SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_ops') INTO role_exists;
    IF role_exists THEN
        EXECUTE 'GRANT USAGE ON SCHEMA governance TO klonet_ops';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA governance TO klonet_ops';
        EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA governance TO klonet_ops';
    END IF;

    SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_migrator') INTO role_exists;
    IF role_exists THEN
        EXECUTE 'GRANT USAGE ON SCHEMA governance TO klonet_migrator';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA governance TO klonet_migrator';
        EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA governance TO klonet_migrator';
    END IF;
END
$$;
