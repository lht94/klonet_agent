-- 0007_memory_maintenance_proposals.sql — 整理提案表（04 计划阶段 5）
--
-- 对应计划 §6.4。ConsolidationJob 只**产生提案**，永远不直接改写正式记忆；
-- 只有显式批准（CLI ``proposals approve``）才通过 ``memory/versioning.py:apply_plan``
-- 落地。这张表就是"未批准的提案永远不会变成正式版本"的可审计凭证。
--
-- 设计要点：
--   * **与 0006 同一个 schema**（`memory_maintenance`）：它同样是系统表，不是
--     用户作用域数据。因此**没有 RLS**，租户过滤由仓库层强制（见
--     ``memory/maintenance/proposals.py`` 的 ``ProposalStore``：构造必须绑定
--     Tenant，每条 SQL 都带 user_id/project_id 谓词）。
--   * **唯一指纹**（``fingerprint``）保证"同一候选集合不会重复创建 pending
--     proposal"——用 partial unique index，而不是让应用层先查再插（后者在并发
--     Worker 下必然漏）。
--   * **状态机落库**：用触发器拒绝非法状态转换，别只写在 Python 里。理由同
--     03 计划阶段 5 的教训——"只写注释的状态机等于没写"。
--   * ``source_fingerprints`` 是**乐观锁**：创建时记录每个来源的
--     ``(status, active_version_id, content_hash)``；apply 时重新加载比对，
--     不一致就让提案过期重算，而不是把已经变了的内容按旧计划改掉。
--   * 表里**不存正文**：``suggested_action`` 只放结构化的决策与来源引用；
--     真正要写入的正文由 apply 路径按当前来源重新组装。这样即使数据库被翻，
--     也读不到比 memory_versions 更多的内容。

CREATE SCHEMA IF NOT EXISTS memory_maintenance;

SET LOCAL search_path TO memory_maintenance, public;

CREATE TABLE IF NOT EXISTS memory_maintenance.memory_maintenance_proposals (
    proposal_id        uuid        PRIMARY KEY,
    -- 谁产生的（目前只有 consolidation；阶段 6 之后可能有人工导入）。
    job_name           text        NOT NULL DEFAULT 'consolidation'
                                   CHECK (job_name <> ''),
    -- 租户三元组。``project_id`` 为 NULL 表示 user 作用域。
    user_id            text        NOT NULL CHECK (user_id <> ''),
    project_id         text,
    scope              text        NOT NULL
                                   CHECK (scope IN ('user', 'project', 'shared_ops')),
    proposal_type      text        NOT NULL
                                   CHECK (proposal_type IN ('noop', 'merge', 'supersede', 'conflict')),
    -- 至少两条：单条记忆不构成"整理"。
    source_memory_ids  uuid[]      NOT NULL
                                   CHECK (array_length(source_memory_ids, 1) >= 2),
    -- 结构化建议。**不允许**出现可以直接执行的 repository 调用；apply 路径会
    -- 把它翻译成 ConsolidationPlan 再走唯一写入出口。
    suggested_action   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    reason_codes       text[]      NOT NULL DEFAULT '{}',
    evidence_refs      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    -- 候选集合的稳定指纹（类型 + 排序后的 memory_id 集合）。并发去重靠它。
    fingerprint        text        NOT NULL CHECK (fingerprint <> ''),
    -- 生成这条提案时使用的策略/模型版本：换模型后旧提案要靠它判断"是否还值得
    -- 按原计划执行"。
    policy_version     text        NOT NULL,
    model_version      text,
    status             text        NOT NULL DEFAULT 'pending'
                                   CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'expired')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    reviewed_at        timestamptz,
    -- apply 成功后的落库版本（审计：这条提案最终改了哪几个版本）。
    applied_version_ids uuid[]     NOT NULL DEFAULT '{}',
    -- 乐观锁快照：{memory_id: {status, active_version_id, content_hash}}。
    source_fingerprints jsonb      NOT NULL DEFAULT '{}'::jsonb,
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- 同一租户下，**尚未尘埃落定**的提案对同一候选集合只能有一条。
-- pending 与 approved 都算"未尘埃落定"：已批准但还没 apply 的提案如果被
-- 重复创建，运维会看到两条一模一样的待办，并可能apply 两次。
-- 用 ``COALESCE(project_id, '')`` 而不是裸 project_id：NULL 在唯一索引里互不
-- 相等，裸列会让"user 作用域"的提案完全失去去重。
CREATE UNIQUE INDEX IF NOT EXISTS memory_maintenance_proposals_open_fingerprint_idx
    ON memory_maintenance.memory_maintenance_proposals
        (user_id, COALESCE(project_id, ''), fingerprint)
    WHERE status IN ('pending', 'approved');

-- 运维列表按 (status, created_at)：先看最新的 pending。
CREATE INDEX IF NOT EXISTS memory_maintenance_proposals_status_idx
    ON memory_maintenance.memory_maintenance_proposals (status, created_at DESC);

-- 按租户查（CLI 与 Job 都按租户过滤）。
CREATE INDEX IF NOT EXISTS memory_maintenance_proposals_tenant_idx
    ON memory_maintenance.memory_maintenance_proposals
        (user_id, COALESCE(project_id, ''), created_at DESC);

-- updated_at 自动维护（与 0006 同一个函数）。
CREATE OR REPLACE FUNCTION memory_maintenance.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS memory_maintenance_proposals_touch
    ON memory_maintenance.memory_maintenance_proposals;
CREATE TRIGGER memory_maintenance_proposals_touch
    BEFORE UPDATE ON memory_maintenance.memory_maintenance_proposals
    FOR EACH ROW EXECUTE FUNCTION memory_maintenance.touch_updated_at();

-- --------------------------------------------------------------------------- --
-- 状态机（落库，不只写在注释里）
-- --------------------------------------------------------------------------- --
--
--     pending  → approved | rejected | expired
--     approved → applied  | expired
--     applied / rejected / expired 是终态
--
-- 为什么"applied 是终态"：一条提案落地之后再改它的状态，等于把"这次改动是否
-- 经过批准"的审计依据擦掉。要再整理同一批记忆，应该由下一轮 consolidation
-- 产生**新的**提案（指纹相同但老的那条已经是 applied，partial unique index
-- 放行）。
--
-- 为什么"applied 只能从 approved 来"：这就是"未批准提案导致的正式版本变化
-- 数为 0"这条验收指标的数据库级保证——即使应用层写错，也刷不出这个状态。

CREATE OR REPLACE FUNCTION memory_maintenance.proposals_guard_transition()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'pending'  AND NEW.status IN ('approved', 'rejected', 'expired') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status IN ('applied', 'expired') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION
        'proposal 状态转换非法：% -> %（proposal_id=%）',
        OLD.status, NEW.status, OLD.proposal_id
        USING ERRCODE = '2F002';  -- 与 0003 冻结正文用同一类错误码语义
END
$$;

DROP TRIGGER IF EXISTS memory_maintenance_proposals_transition
    ON memory_maintenance.memory_maintenance_proposals;
CREATE TRIGGER memory_maintenance_proposals_transition
    BEFORE UPDATE OF status ON memory_maintenance.memory_maintenance_proposals
    FOR EACH ROW EXECUTE FUNCTION memory_maintenance.proposals_guard_transition();

-- --------------------------------------------------------------------------- --
-- 角色与授权（与 0006 同一套：maint 写、app/ops 只读）
-- --------------------------------------------------------------------------- --

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_maint') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA memory_maintenance TO klonet_maint';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON
                     memory_maintenance.memory_maintenance_proposals TO klonet_maint';
        EXECUTE 'GRANT EXECUTE ON FUNCTION
                     memory_maintenance.proposals_guard_transition() TO klonet_maint';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_app') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA memory_maintenance TO klonet_app';
        EXECUTE 'GRANT SELECT ON
                     memory_maintenance.memory_maintenance_proposals TO klonet_app';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_ops') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA memory_maintenance TO klonet_ops';
        EXECUTE 'GRANT SELECT ON
                     memory_maintenance.memory_maintenance_proposals TO klonet_ops';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_migrator') THEN
        EXECUTE 'GRANT ALL ON
                     memory_maintenance.memory_maintenance_proposals TO klonet_migrator';
    END IF;
END
$$;
