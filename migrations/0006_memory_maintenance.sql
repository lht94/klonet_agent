-- 0006_memory_maintenance.sql — 记忆生命周期维护调度表
-- （通用功能升级计划 04 阶段 1）
--
-- 对应计划 §5.2（调度状态表）与 §5.3（租约与并发）：
--   * memory_maintenance_jobs   — 调度与 cursor 真相，FOR UPDATE SKIP LOCKED 领取
--   * memory_maintenance_runs   — 单次执行的账本（计划/拒绝/失败原因聚合）
--
-- 设计要点：
--   * **独立 schema**（`memory_maintenance`），不与 `public`（业务记忆）
--     和 `governance`（运行事件）混淆。三套职责：业务数据 / 运行事件 /
--     维护调度，分别在不同 schema。
--   * **没有 RLS**——这些是系统表，不是用户/项目作用域数据。
--     访问控制改由角色：`klonet_maint`（Worker 专用，写）、
--     `klonet_app` / `klonet_ops`（只读，给 health snapshot）。
--   * **所有时间戳走 PG now()**：worker 进程时钟可能漂移，但
--     lease 判定必须以数据库 `now()` 为准。
--   * **不在表中保存秘密、原始记忆正文或模型响应**——
--     runs.error_summary 只放 reason_code 与聚合值；正文在 02 阶段 4 的
--     memory_write_candidates 那边，不在这里复制一份。
--   * **FOR UPDATE SKIP LOCKED** 配 advisory lock：前者领取 Job，
--     后者给"全局不可并行"的子操作（迁移切换、批次重嵌入）用。
--   * 状态机：`runs.status ∈ {running, succeeded, failed, abandoned}`。
--     `running` 是当前正在跑；`abandoned` 是 worker 异常退出后租约过期
--     留下的"无人续约"标记，下一次 claim 时通过 lease_expires_at 判定。
--   * runs.started_at 用 timestamptz NOT NULL DEFAULT now()，与 jobs.last_started_at
--     区分：后者是上次开始时间（写在 jobs 上），前者是本 run 的开始时间
--     （写在 runs 上）。两者都靠 PG 时钟，不靠 worker 本机。

CREATE SCHEMA IF NOT EXISTS memory_maintenance;

-- 把本事务的 search_path 优先指向 memory_maintenance；以后 CREATE TABLE /
-- INDEX / FUNCTION 不必再写完整 schema 名。事务结束自动恢复（用 SET LOCAL）。
SET LOCAL search_path TO memory_maintenance, public;

-- --------------------------------------------------------------------------- --
-- memory_maintenance_jobs：调度真相
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS memory_maintenance.memory_maintenance_jobs (
    job_name              text        PRIMARY KEY
                                      CHECK (job_name <> ''),
    enabled               boolean     NOT NULL DEFAULT true,
    -- 默认调度周期（秒）。Worker 跑时取此值；具体工作可临时调大。
    schedule_seconds      integer     NOT NULL
                                      CHECK (schedule_seconds > 0),
    -- 下次应跑时间。claim_due_job 找 ``next_run_at <= now() AND enabled``。
    next_run_at           timestamptz NOT NULL,
    -- 批次推进光标。形态由各 Job 自己定义（一般是 (col, id) 复合）。
    -- 不在 schema 层校验形态——``memory/maintenance/repository.py`` 实现层负责。
    cursor                jsonb,
    -- 当前 lease；过期可被重新领取。
    lease_owner           text,
    lease_expires_at      timestamptz,
    -- 历史时间戳，全部由 PG 写入。
    last_started_at       timestamptz,
    last_succeeded_at     timestamptz,
    -- 连续失败次数；超阈值告警并暂停 Job（具体阈值在 health.py）。
    consecutive_failures  integer     NOT NULL DEFAULT 0
                                      CHECK (consecutive_failures >= 0),
    -- 临时覆盖默认 schedule_seconds / batch_size / 其它 Job 特定参数。
    config                jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- updated_at 触发器在 claim_due_job / heartbeat / complete / fail
    -- 写时自动更新（不靠应用层维护）。
    updated_at            timestamptz NOT NULL DEFAULT now()
);

-- 加速 claim_due_job 的扫描：``WHERE enabled AND next_run_at <= now()``。
-- 走 (enabled, next_run_at) 复合：enabled 频度高但取值少（0/1），第二列才
-- 决定选择度。
CREATE INDEX IF NOT EXISTS memory_maintenance_jobs_due_idx
    ON memory_maintenance.memory_maintenance_jobs (enabled, next_run_at);

-- 仅当 lease_owner 非空时让 lease 检索走索引——大部分行 lease 列为 NULL，
-- 复合索引第二列的选择度才有用。
CREATE INDEX IF NOT EXISTS memory_maintenance_jobs_lease_idx
    ON memory_maintenance.memory_maintenance_jobs (lease_owner, lease_expires_at)
    WHERE lease_owner IS NOT NULL;

-- --------------------------------------------------------------------------- --
-- memory_maintenance_runs：单次执行的账本
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS memory_maintenance.memory_maintenance_runs (
    run_id            uuid        PRIMARY KEY,
    job_name          text        NOT NULL
                                  REFERENCES memory_maintenance.memory_maintenance_jobs (job_name)
                                  ON DELETE CASCADE,
    worker_id         text        NOT NULL
                                  CHECK (worker_id <> ''),
    -- running → succeeded / failed / abandoned。
    -- abandoned 由清理 Job（阶段 3 之后）写：租约过期且 runs 还在 running。
    status            text        NOT NULL
                                  CHECK (status IN ('running', 'succeeded', 'failed', 'abandoned')),
    started_at        timestamptz NOT NULL,
    finished_at       timestamptz,
    -- 聚合统计，由 Job.run() 返回后写入；不在 schema 层强制非负
    -- （scanned/changed/failed 都可能为 0）。
    scanned           integer     NOT NULL DEFAULT 0,
    changed           integer     NOT NULL DEFAULT 0,
    proposed          integer     NOT NULL DEFAULT 0,
    failed            integer     NOT NULL DEFAULT 0,
    -- 仅 failed/abandoned 才有这两列。reason_code 是稳定短串，summary 是
    -- 人可读但**不**含正文/token/连接密码。
    error_code        text,
    error_summary     text,
    details           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- 同一 job 同一时刻只有一个 running；用 partial unique index 兜底
    -- 防止历史 bug 让两个 running 共存。
    CONSTRAINT memory_maintenance_runs_summary_no_secret_len
        CHECK (error_summary IS NULL OR length(error_summary) <= 1000)
);

CREATE UNIQUE INDEX IF NOT EXISTS memory_maintenance_runs_one_running_idx
    ON memory_maintenance.memory_maintenance_runs (job_name)
    WHERE status = 'running';

-- 检索与健康查询的常用 pattern：按 job_name + started_at desc。
CREATE INDEX IF NOT EXISTS memory_maintenance_runs_job_idx
    ON memory_maintenance.memory_maintenance_runs (job_name, started_at DESC);

-- 健康报告查"最近失败的 run"，按 status 过滤。
CREATE INDEX IF NOT EXISTS memory_maintenance_runs_status_idx
    ON memory_maintenance.memory_maintenance_runs (status, finished_at DESC)
    WHERE status IN ('failed', 'abandoned');

-- updated_at 自动维护
CREATE OR REPLACE FUNCTION memory_maintenance.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS memory_maintenance_jobs_touch ON memory_maintenance.memory_maintenance_jobs;
CREATE TRIGGER memory_maintenance_jobs_touch
    BEFORE UPDATE ON memory_maintenance.memory_maintenance_jobs
    FOR EACH ROW EXECUTE FUNCTION memory_maintenance.touch_updated_at();

-- --------------------------------------------------------------------------- --
-- 角色与授权
-- --------------------------------------------------------------------------- --

DO $$
DECLARE
    role_exists boolean;
BEGIN
    -- 新建 klonet_maint 角色（Worker 专用）。缺 CREATEROLE 时只告警
    -- 不中断——schema 仍落地，由 DBA 补建角色后重跑 0006 即可。
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_maint') THEN
            CREATE ROLE klonet_maint LOGIN;
        END IF;
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE WARNING '[0006] 当前账号缺少 CREATEROLE 权限，跳过 klonet_maint 角色创建：%', SQLERRM;
    END;

    -- klonet_maint：全权读写
    SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_maint') INTO role_exists;
    IF role_exists THEN
        EXECUTE 'GRANT USAGE ON SCHEMA memory_maintenance TO klonet_maint';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON
                     memory_maintenance.memory_maintenance_jobs,
                     memory_maintenance.memory_maintenance_runs
                 TO klonet_maint';
    ELSE
        RAISE WARNING '[0006] klonet_maint 角色不存在，Worker 写权限未创建（fail-closed）';
    END IF;

    -- klonet_app：只读（health snapshot 需要读 jobs.last_succeeded_at
    -- 与 runs.status，但不能让在线请求写）。
    SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_app') INTO role_exists;
    IF role_exists THEN
        EXECUTE 'GRANT USAGE ON SCHEMA memory_maintenance TO klonet_app';
        EXECUTE 'GRANT SELECT ON
                     memory_maintenance.memory_maintenance_jobs,
                     memory_maintenance.memory_maintenance_runs
                 TO klonet_app';
    END IF;

    -- klonet_ops：只读（与 klonet_app 相同；权限策略不区分这两者对
    -- 维护表的可见性）。
    SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_ops') INTO role_exists;
    IF role_exists THEN
        EXECUTE 'GRANT USAGE ON SCHEMA memory_maintenance TO klonet_ops';
        EXECUTE 'GRANT SELECT ON
                     memory_maintenance.memory_maintenance_jobs,
                     memory_maintenance.memory_maintenance_runs
                 TO klonet_ops';
    END IF;

    -- klonet_migrator：全权（迁移执行账号；既能跑迁移也能排查）。
    SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_migrator') INTO role_exists;
    IF role_exists THEN
        EXECUTE 'GRANT USAGE ON SCHEMA memory_maintenance TO klonet_migrator';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON
                     memory_maintenance.memory_maintenance_jobs,
                     memory_maintenance.memory_maintenance_runs
                 TO klonet_migrator';
    END IF;
END
$$;

-- --------------------------------------------------------------------------- --
-- 迁移握手：把已知 Job 名字先 INSERT 一行（enabled=false），让 schema 落地
-- 后 worker 起来时只需 UPDATE；不会因 ON CONFLICT 报错。
-- --------------------------------------------------------------------------- --

INSERT INTO memory_maintenance.memory_maintenance_jobs
    (job_name, enabled, schedule_seconds, next_run_at)
VALUES
    ('embedding_outbox', false, 10,    '2099-01-01T00:00:00+00:00'),
    ('expiration',       false, 3600,  '2099-01-01T00:00:00+00:00'),
    ('purge',            false, 86400, '2099-01-01T00:00:00+00:00'),
    ('consolidation',    false, 86400, '2099-01-01T00:00:00+00:00'),
    ('reembedding',      false, 86400, '2099-01-01T00:00:00+00:00'),
    ('health_report',    false, 60,    '2099-01-01T00:00:00+00:00')
ON CONFLICT (job_name) DO NOTHING;
