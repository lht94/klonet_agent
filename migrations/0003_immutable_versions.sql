-- 0003_immutable_versions.sql — 版本不可变 + verified 列
--
-- 对应 `通用功能升级计划/02-记忆系统升级计划.md` 阶段 2 清单：
--   * "禁止直接修改既有 memory_versions 正文"
--   * "实现 episode/fact/preference 三类领域校验"（其中 verified 的说法需要落库）
--
-- 为什么不可变要落到数据库层：
--   应用层"没有提供修改正文的方法"只是约定，任何一次手写 UPDATE、一次数据修复脚本、
--   一次 migration 都可能绕过它。计划 §6.2 的核心承诺是"正文只存在于版本里，
--   改一条记忆等于新增版本，不覆盖历史"，这个承诺必须由数据库来守。
--
-- 允许变化的三类列（触发器放行，其余一律拒绝）：
--   * valid_to            —— 替代/过期时结束有效期，是"当时有效的事实"的唯一依据；
--   * embedding 三列       —— 阶段 4 的 worker 异步补算，正文事务不写向量；
--   * verified            —— 只允许 false → true（后来补齐了证据）。
--
-- 幂等性：ADD COLUMN IF NOT EXISTS / CREATE OR REPLACE / DROP TRIGGER IF EXISTS，
-- 重复执行无副作用。

-- --------------------------------------------------------------------------- --
-- verified：这条版本的值是否已被证据确认
-- --------------------------------------------------------------------------- --
--
-- §7.1："verified 必须存在用户明确陈述或成功工具证据；仅 assistant 自述不能标为已验证。"
-- 这个判断不能从来源自动推导（"来源里有 user_statement"不等于"这条陈述确认了当前这个
-- 值"），所以显式落库，由应用层在写入时按类型校验（见 memory/domain.py 的
-- ensure_verification_supported）。

ALTER TABLE memory_versions
    ADD COLUMN IF NOT EXISTS verified boolean NOT NULL DEFAULT false;

-- --------------------------------------------------------------------------- --
-- 版本不可变触发器
-- --------------------------------------------------------------------------- --

CREATE OR REPLACE FUNCTION memory_versions_guard_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- 冻结列：身份、正文、判重哈希、时间来源、分词结果。
    -- 用 IS DISTINCT FROM 而不是 <>：这两列的语义在 NULL 上不同，
    -- 而 summary / metadata 允许为 NULL。
    IF NEW.id            IS DISTINCT FROM OLD.id
    OR NEW.memory_id     IS DISTINCT FROM OLD.memory_id
    OR NEW.version       IS DISTINCT FROM OLD.version
    OR NEW.content       IS DISTINCT FROM OLD.content
    OR NEW.summary       IS DISTINCT FROM OLD.summary
    OR NEW.metadata      IS DISTINCT FROM OLD.metadata
    OR NEW.lexical_text  IS DISTINCT FROM OLD.lexical_text
    OR NEW.content_hash  IS DISTINCT FROM OLD.content_hash
    OR NEW.observed_at   IS DISTINCT FROM OLD.observed_at
    OR NEW.valid_from    IS DISTINCT FROM OLD.valid_from
    OR NEW.created_at    IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'memory_versions 正文不可修改（memory_id=%, version=%）',
            OLD.memory_id, OLD.version
            USING ERRCODE = '2F002',
                  HINT = '要修正内容请追加新版本；只允许修改 valid_to、'
                         'embedding/embedding_model/embedding_version，'
                         '以及 verified 从 false 变为 true';
    END IF;

    -- verified 是单向开关：证据到了可以打勾，但不能被谁悄悄撤销。
    IF NEW.verified IS DISTINCT FROM OLD.verified
       AND NOT (OLD.verified IS FALSE AND NEW.verified IS TRUE)
    THEN
        RAISE EXCEPTION
            'memory_versions.verified 只能从 false 变成 true'
            USING ERRCODE = '2F002';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS memory_versions_immutable ON memory_versions;
CREATE TRIGGER memory_versions_immutable
    BEFORE UPDATE ON memory_versions
    FOR EACH ROW
    EXECUTE FUNCTION memory_versions_guard_mutation();
