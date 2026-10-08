-- 0004_governance.sql — 运行治理 schema（通用功能升级计划 03，阶段 1）
--
-- 对应计划 §4.4（存储方式）与 §5（数据模型建议）：
--   governance.runs / turns / tasks / steps / runtime_events /
--   failures / model_calls / tool_calls / redactions
--
-- 设计要点：
--   * runtime_events 是**追加写**的事实账本；tasks/steps/failures 等表保存
--     当前投影。两者由应用层在同一事务里提交（见 runtime/governance/postgres.py），
--     数据库层用约束兜底不变量。
--   * 与记忆系统共用租户变量：RLS 策略读 `app.user_id` / `app.project_id`
--     （见 memory/database.py 的 tenant_session），部署上仍是一套数据库、
--     两套职责清晰的 schema。
--   * 计划 §5.1 示例把所有权字段写成 uuid；本项目沿用记忆系统的 text 型
--     user_id/project_id（"alice"、"default" 这类业务标识），避免两套 schema
--     出现两种租户类型。
--   * 不可变不凌驾于删除权：payload 中的敏感内容由 PrivacyGateway 在写入前
--     脱敏/拒绝；物理删除走未来的 admin 路径，账本里只留不含原文的 tombstone。
--
-- 本文件不含 BEGIN/COMMIT，事务由迁移执行器负责。幂等：全部 IF NOT EXISTS。

-- 外键延迟（DEFERRABLE INITIALLY DEFERRED）：事件与投影同事务提交时，
-- 事件行（引用 task/step/turn）先于投影行写入；延迟到 COMMIT 才校验，
-- 事件引用与投影在同一事务里互相满足。事务回滚时两者一起消失，
-- 不存在"事件引用悬空投影"或反之的半套状态。

CREATE SCHEMA IF NOT EXISTS governance;

-- --------------------------------------------------------------------------- --
-- runs：一次端到端执行
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS governance.runs (
    run_id          text        PRIMARY KEY,
    user_id         text        NOT NULL CHECK (user_id <> ''),
    project_id      text,
    session_id      text,
    mode            text,
    schema_version  integer     NOT NULL DEFAULT 1,
    started_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT runs_project_id_not_empty
        CHECK (project_id IS NULL OR project_id <> '')
);

-- --------------------------------------------------------------------------- --
-- turns：一次用户输入及其处理过程
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS governance.turns (
    turn_id         text        PRIMARY KEY,
    run_id          text        NOT NULL REFERENCES governance.runs (run_id),
    user_id         text        NOT NULL CHECK (user_id <> ''),
    project_id      text,
    session_id      text,
    started_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT turns_project_id_not_empty
        CHECK (project_id IS NULL OR project_id <> '')
);

-- --------------------------------------------------------------------------- --
-- tasks：任务当前投影（权威状态机，计划 §4.1）
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS governance.tasks (
    task_id          text        PRIMARY KEY,
    run_id           text        NOT NULL REFERENCES governance.runs (run_id),
    user_id          text        NOT NULL CHECK (user_id <> ''),
    project_id       text,
    content          text        NOT NULL CHECK (content <> ''),
    status           text        NOT NULL DEFAULT 'pending'
                                 CHECK (status IN
                                     ('pending', 'running', 'blocked',
                                      'completed', 'cancelled')),
    priority         integer     NOT NULL DEFAULT 0,
    ordinal          integer     NOT NULL DEFAULT 0,
    blocked_reason   text,
    version          integer     NOT NULL CHECK (version > 0),
    idempotency_key  text        UNIQUE,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tasks_project_id_not_empty
        CHECK (project_id IS NULL OR project_id <> '')
);

CREATE INDEX IF NOT EXISTS tasks_scope_idx
    ON governance.tasks (user_id, project_id, run_id);

-- --------------------------------------------------------------------------- --
-- steps：步骤当前投影
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS governance.steps (
    step_id          text        PRIMARY KEY,
    task_id          text        NOT NULL REFERENCES governance.tasks (task_id) DEFERRABLE INITIALLY DEFERRED,
    run_id           text        NOT NULL REFERENCES governance.runs (run_id),
    user_id          text        NOT NULL CHECK (user_id <> ''),
    project_id       text,
    content          text        NOT NULL CHECK (content <> ''),
    status           text        NOT NULL DEFAULT 'pending'
                                 CHECK (status IN
                                     ('pending', 'running', 'succeeded',
                                      'failed', 'skipped')),
    reason_code      text,
    version          integer     NOT NULL CHECK (version > 0),
    idempotency_key  text        UNIQUE,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT steps_project_id_not_empty
        CHECK (project_id IS NULL OR project_id <> '')
);

CREATE INDEX IF NOT EXISTS steps_task_idx
    ON governance.steps (task_id, run_id);

-- --------------------------------------------------------------------------- --
-- runtime_events：追加写事件账本（唯一权威事实，计划 §4.4/§5.1）
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS governance.runtime_events (
    event_id          uuid        PRIMARY KEY,
    event_type        text        NOT NULL CHECK (event_type <> ''),
    schema_version    integer     NOT NULL DEFAULT 1,
    user_id           text        CHECK (user_id IS NULL OR user_id <> ''),
    project_id        text,
    session_id        text,
    run_id            text        NOT NULL REFERENCES governance.runs (run_id),
    turn_id           text        REFERENCES governance.turns (turn_id)
                                  DEFERRABLE INITIALLY DEFERRED,
    task_id           text        REFERENCES governance.tasks (task_id)
                                  DEFERRABLE INITIALLY DEFERRED,
    step_id           text        REFERENCES governance.steps (step_id)
                                  DEFERRABLE INITIALLY DEFERRED,
    parent_event_id   uuid        REFERENCES governance.runtime_events (event_id)
                                  DEFERRABLE INITIALLY DEFERRED,
    idempotency_key   text        UNIQUE,
    actor_type        text        NOT NULL
                                  CHECK (actor_type IN
                                      ('system', 'user', 'model', 'tool')),
    actor_id          text,
    payload           jsonb       NOT NULL,
    privacy_class     text        NOT NULL DEFAULT 'internal'
                                  CHECK (privacy_class IN
                                      ('public', 'internal', 'sensitive', 'secret')),
    occurred_at       timestamptz NOT NULL,
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT runtime_events_project_id_not_empty
        CHECK (project_id IS NULL OR project_id <> '')
);

-- 审计/回放主查询：按运行取时间线；按类型过滤。
CREATE INDEX IF NOT EXISTS runtime_events_run_time_idx
    ON governance.runtime_events (run_id, occurred_at);
CREATE INDEX IF NOT EXISTS runtime_events_type_idx
    ON governance.runtime_events (event_type, occurred_at);

-- --------------------------------------------------------------------------- --
-- failures：失败当前投影（计划 §4.2/§5.3）
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS governance.failures (
    failure_id               text        PRIMARY KEY,
    run_id                   text        NOT NULL REFERENCES governance.runs (run_id),
    user_id                  text        NOT NULL CHECK (user_id <> ''),
    project_id               text,
    stage                    text        NOT NULL CHECK (stage <> ''),
    error_class              text        NOT NULL CHECK (error_class <> ''),
    message                  text        NOT NULL DEFAULT '',
    retryable                boolean     NOT NULL DEFAULT false,
    status                   text        NOT NULL DEFAULT 'active'
                                         CHECK (status IN
                                             ('active', 'resolved', 'verified', 'dismissed')),
    attempt_count            integer     NOT NULL DEFAULT 1 CHECK (attempt_count > 0),
    task_id                  text        REFERENCES governance.tasks (task_id)
                                         DEFERRABLE INITIALLY DEFERRED,
    step_id                  text        REFERENCES governance.steps (step_id)
                                         DEFERRABLE INITIALLY DEFERRED,
    turn_id                  text        REFERENCES governance.turns (turn_id)
                                         DEFERRABLE INITIALLY DEFERRED,
    root_cause_hypothesis    text,
    resolution_action        text,
    verification_evidence_ids jsonb      NOT NULL DEFAULT '[]'::jsonb,
    idempotency_key          text        UNIQUE,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    -- 计划 §5.3：同一步骤同类失败只允许一个活跃记录。
    -- 用表达式索引兜底（active 记录 uniqueness）；历史 resolved 记录不受限。
    CONSTRAINT failures_project_id_not_empty
        CHECK (project_id IS NULL OR project_id <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS failures_active_step_class_idx
    ON governance.failures (step_id, error_class)
    WHERE status = 'active' AND step_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS failures_active_task_class_idx
    ON governance.failures (task_id, error_class)
    WHERE status = 'active' AND step_id IS NULL AND task_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS failures_run_status_idx
    ON governance.failures (run_id, status);

-- --------------------------------------------------------------------------- --
-- model_calls / tool_calls：调用生命周期投影（不存秘密与思维链）
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS governance.model_calls (
    call_id       text        PRIMARY KEY,
    run_id        text        NOT NULL REFERENCES governance.runs (run_id),
    user_id       text        NOT NULL CHECK (user_id <> ''),
    project_id    text,
    model         text        NOT NULL CHECK (model <> ''),
    outcome       text        NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
    total_tokens  integer     NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    duration_ms   integer     NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    turn_id       text        REFERENCES governance.turns (turn_id)
                              DEFERRABLE INITIALLY DEFERRED,
    error_class   text,
    occurred_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS model_calls_run_idx
    ON governance.model_calls (run_id, occurred_at);

CREATE TABLE IF NOT EXISTS governance.tool_calls (
    call_id       text        PRIMARY KEY,
    run_id        text        NOT NULL REFERENCES governance.runs (run_id),
    user_id       text        NOT NULL CHECK (user_id <> ''),
    project_id    text,
    tool_name     text        NOT NULL CHECK (tool_name <> ''),
    outcome       text        NOT NULL CHECK (outcome IN
                                  ('not_started', 'started', 'succeeded',
                                   'failed', 'outcome_unknown')),
    duration_ms   integer     NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    args_preview  jsonb       NOT NULL DEFAULT '{}'::jsonb,
    turn_id       text        REFERENCES governance.turns (turn_id)
                              DEFERRABLE INITIALLY DEFERRED,
    error_class   text,
    occurred_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tool_calls_run_idx
    ON governance.tool_calls (run_id, occurred_at);

-- --------------------------------------------------------------------------- --
-- redactions：脱敏留痕（只记规则、类别与哈希，绝不存原文）
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS governance.redactions (
    id             bigserial   PRIMARY KEY,
    event_id       uuid        NOT NULL REFERENCES governance.runtime_events (event_id),
    user_id        text        NOT NULL CHECK (user_id <> ''),
    project_id     text,
    rule_id        text        NOT NULL CHECK (rule_id <> ''),
    category       text        NOT NULL CHECK (category <> ''),
    content_hash   text        NOT NULL CHECK (content_hash <> ''),
    privacy_class  text        NOT NULL DEFAULT 'sensitive'
                               CHECK (privacy_class IN
                                   ('sensitive', 'secret')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT redactions_project_id_not_empty
        CHECK (project_id IS NULL OR project_id <> '')
);

CREATE INDEX IF NOT EXISTS redactions_event_idx
    ON governance.redactions (event_id);

-- --------------------------------------------------------------------------- --
-- RLS：与记忆系统同一套租户变量（app.user_id / app.project_id）
-- --------------------------------------------------------------------------- --

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'runs', 'turns', 'tasks', 'steps', 'runtime_events',
        'failures', 'model_calls', 'tool_calls', 'redactions'
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

-- --------------------------------------------------------------------------- --
-- 授权：角色沿用 0002 建的三个角色；缺角色时告警不中断（与 0002 同策略）
-- --------------------------------------------------------------------------- --

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
        RAISE WARNING '[0004] klonet_app 不存在，governance 授权未创建（fail-closed）';
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
