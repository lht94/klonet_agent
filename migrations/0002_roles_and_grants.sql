-- 0002_roles_and_grants.sql — 角色、授权与 shared_ops 放行
--
-- 对应计划 §5.3「作用域和数据库安全」与阶段 1 清单的
-- 「使用普通应用角色、Ops 共享角色和管理员迁移角色验证权限」。
--
-- 三个角色：
--   klonet_migrator  迁移/备份管理员。理想情况下由它执行 0001/0002 从而成为表 owner；
--                    不与在线请求共用连接池。
--   klonet_app       在线请求角色。无 BYPASSRLS，受 RLS 约束，**读不到** shared_ops。
--   klonet_ops       Ops 服务角色。除普通租户视图外，额外放行 scope='shared_ops'。
--
-- 本文件不含 BEGIN/COMMIT，事务由迁移执行器负责；手工执行请用
--   psql --single-transaction -f migrations/0002_roles_and_grants.sql
--
-- 幂等性：角色用判存创建，授权重复执行无副作用。
-- 密码不在这里设置（明文进版本库是禁忌），部署时单独执行：
--   ALTER ROLE klonet_app PASSWORD '...';
--
-- 若执行迁移的账号没有 CREATEROLE 权限，本文件只告警不中断——schema 先落地，
-- 由 DBA 补建角色后重跑本文件即可。此时 shared_ops 的角色级放行不会被创建，
-- Ops 读共享记忆会失败（fail-closed），不会静默降级成"人人可读"。

DO $migration$
DECLARE
    role_exists boolean;
BEGIN
    -- ----------------------------------------------------------------- 角色 --
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_migrator') THEN
            CREATE ROLE klonet_migrator LOGIN;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_app') THEN
            CREATE ROLE klonet_app LOGIN;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_ops') THEN
            CREATE ROLE klonet_ops LOGIN;
        END IF;
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE WARNING '[0002] 当前账号缺少 CREATEROLE 权限，跳过角色创建：%', SQLERRM;
    END;

    -- --------------------------------------------------------------- 授权 --
    SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_app') INTO role_exists;
    IF role_exists THEN
        EXECUTE 'GRANT USAGE ON SCHEMA public TO klonet_app';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON
                     memory_records, memory_versions, memory_sources,
                     memory_relations, memory_write_candidates, memory_embedding_outbox
                 TO klonet_app';
        -- memory_sources.id 是 bigserial。
        EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO klonet_app';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public
                     GRANT USAGE, SELECT ON SEQUENCES TO klonet_app';
    END IF;

    SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_ops') INTO role_exists;
    IF role_exists THEN
        EXECUTE 'GRANT USAGE ON SCHEMA public TO klonet_ops';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON
                     memory_records, memory_versions, memory_sources,
                     memory_relations, memory_write_candidates, memory_embedding_outbox
                 TO klonet_ops';
        EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO klonet_ops';

        -- shared_ops 的放行挂在角色上，而不是挂在会话可自设的 GUC 上。
        -- 这条 policy 与 memory_records_tenant 是 OR 关系：
        --   klonet_app 命中 memory_records_tenant（读不到 shared_ops）
        --   klonet_ops 额外命中本 policy（可以读写 shared_ops）
        EXECUTE 'DROP POLICY IF EXISTS memory_records_shared_ops ON memory_records';
        EXECUTE 'CREATE POLICY memory_records_shared_ops ON memory_records
                     FOR ALL TO klonet_ops
                     USING (scope = ''shared_ops'')
                     WITH CHECK (scope = ''shared_ops'')';
    END IF;

    SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klonet_migrator') INTO role_exists;
    IF role_exists THEN
        EXECUTE 'GRANT USAGE ON SCHEMA public TO klonet_migrator';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON
                     memory_records, memory_versions, memory_sources,
                     memory_relations, memory_write_candidates, memory_embedding_outbox
                 TO klonet_migrator';
        EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO klonet_migrator';
    END IF;
END
$migration$;
