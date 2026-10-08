# Klonet 记忆系统专项评测

- cases: 9
- 真库：可用
- 阈值冻结时间：2026-10-08

## 三条路径总览

| arm | 查询数 | recall@k | precision@k | 过期误召回率 | 跨租户泄漏率 | 平均 token |
| --- | --- | --- | --- | --- | --- | --- |
| markdown | 10 | 1.0000 | 0.6000 | 0.3000 | 0.0000 | 26.10 |
| db_keyword | 10 | 0.8000 | 0.7500 | 0.0000 | 0.0000 | 13.00 |
| db_hybrid | 10 | 1.0000 | 0.7500 | 0.0000 | 0.0000 | 22.80 |

- 冲突标注率（混合检索，仅冲突用例）：1.0000
- 语义用例由混合检索赢下：1/1

## 阈值核验

| 检查项 | 实测 | 阈值 | 结果 |
| --- | --- | --- | --- |
| hybrid_recall_at_k_min | 1.0 | 0.9 | PASS |
| hybrid_precision_at_k_min | 0.75 | 0.6 | PASS |
| keyword_recall_at_k_min | 0.8 | 0.6 | PASS |
| expired_false_recall_max | 0.0 | 0.0 | PASS |
| cross_tenant_leakage_max | 0.0 | 0.0 | PASS |
| conflict_flag_rate_min | 1.0 | 1.0 | PASS |
| hybrid_token_ratio_vs_markdown_max | 0.8736 | 1.0 | PASS |
| hybrid_recall_not_below_keyword | 0.2 | 0.0 | PASS |
| hybrid_only_cases_won | 1 | 1 | PASS |

## 逐条明细

### mem_single_hop_fact（OK）
- 说明：单跳事实：中文问句应召回对应的项目事实
- `markdown`｜as_of=-｜recall=1.0｜precision=0.3333｜token=47｜过期误召回=否｜泄漏=否｜命中=['runtime', 'web_port', 'db_engine']
- `db_keyword`｜as_of=-｜recall=1.0｜precision=1.0｜token=17｜过期误召回=否｜泄漏=否｜命中=['runtime']
- `db_hybrid`｜as_of=-｜recall=1.0｜precision=0.3333｜token=47｜过期误召回=否｜泄漏=否｜命中=['runtime', 'db_engine', 'web_port']

### mem_exact_identifier（OK）
- 说明：精确标识符：查询里的 8080 应精确命中端口事实
- `markdown`｜as_of=-｜recall=1.0｜precision=0.5｜token=27｜过期误召回=否｜泄漏=否｜命中=['web_port', 'api_port']
- `db_keyword`｜as_of=-｜recall=1.0｜precision=0.5｜token=27｜过期误召回=否｜泄漏=否｜命中=['api_port', 'web_port']
- `db_hybrid`｜as_of=-｜recall=1.0｜precision=0.5｜token=27｜过期误召回=否｜泄漏=否｜命中=['api_port', 'web_port']

### mem_temporal_as_of（OK）
- 说明：时态：as_of 视图返回当时有效的历史值，当前视图返回新值
- `markdown`｜as_of=2026-03-01T00:00:00+00:00｜recall=1.0｜precision=0.5｜token=18｜过期误召回=是｜泄漏=否｜命中=['py38', 'py311']
- `db_keyword`｜as_of=2026-03-01T00:00:00+00:00｜recall=1.0｜precision=1.0｜token=9｜过期误召回=否｜泄漏=否｜命中=['py38']
- `db_hybrid`｜as_of=2026-03-01T00:00:00+00:00｜recall=1.0｜precision=1.0｜token=9｜过期误召回=否｜泄漏=否｜命中=['py38']
- `markdown`｜as_of=-｜recall=1.0｜precision=0.5｜token=18｜过期误召回=是｜泄漏=否｜命中=['py38', 'py311']
- `db_keyword`｜as_of=-｜recall=1.0｜precision=1.0｜token=9｜过期误召回=否｜泄漏=否｜命中=['py311']
- `db_hybrid`｜as_of=-｜recall=1.0｜precision=1.0｜token=9｜过期误召回=否｜泄漏=否｜命中=['py311']

### mem_expired_not_recalled（OK）
- 说明：过期误召回：已过有效期的记忆在当前视图不可召回
- `markdown`｜as_of=-｜recall=1.0｜precision=0.5｜token=26｜过期误召回=是｜泄漏=否｜命中=['old_flag', 'new_flag']
- `db_keyword`｜as_of=-｜recall=1.0｜precision=1.0｜token=11｜过期误召回=否｜泄漏=否｜命中=['new_flag']
- `db_hybrid`｜as_of=-｜recall=1.0｜precision=1.0｜token=11｜过期误召回=否｜泄漏=否｜命中=['new_flag']

### mem_conflict_flagging（OK）
- 说明：冲突：两条互斥事实都应被召回并被标注 contradicts
- `markdown`｜as_of=-｜recall=1.0｜precision=1.0｜token=16｜过期误召回=否｜泄漏=否｜命中=['f1', 'f2']
- `db_keyword`｜as_of=-｜recall=1.0｜precision=1.0｜token=16｜过期误召回=否｜泄漏=否｜命中=['f2', 'f1']
- `db_hybrid`｜as_of=-｜recall=1.0｜precision=1.0｜token=16｜过期误召回=否｜泄漏=否｜命中=['f2', 'f1']

### mem_multi_hop（OK）
- 说明：多跳：需要同时召回服务本体与其暴露方式两条事实
- `markdown`｜as_of=-｜recall=1.0｜precision=0.6667｜token=45｜过期误召回=否｜泄漏=否｜命中=['svc', 'expose', 'db']
- `db_keyword`｜as_of=-｜recall=1.0｜precision=1.0｜token=30｜过期误召回=否｜泄漏=否｜命中=['expose', 'svc']
- `db_hybrid`｜as_of=-｜recall=1.0｜precision=0.6667｜token=45｜过期误召回=否｜泄漏=否｜命中=['expose', 'svc', 'db']

### mem_preference_recall（OK）
- 说明：偏好：用户偏好应被召回，且不与项目事实混淆
- `markdown`｜as_of=-｜recall=1.0｜precision=0.5｜token=28｜过期误召回=否｜泄漏=否｜命中=['lang_pref', 'db']
- `db_keyword`｜as_of=-｜recall=0.0｜precision=0.0｜token=0｜过期误召回=否｜泄漏=否｜命中=[]
- `db_hybrid`｜as_of=-｜recall=1.0｜precision=0.5｜token=28｜过期误召回=否｜泄漏=否｜命中=['db', 'lang_pref']

### mem_cross_tenant_isolation（OK）
- 说明：跨项目/跨用户隔离：别的租户的同类记忆一条都不能召回
- `markdown`｜as_of=-｜recall=1.0｜precision=1.0｜token=11｜过期误召回=否｜泄漏=否｜命中=['mine']
- `db_keyword`｜as_of=-｜recall=1.0｜precision=1.0｜token=11｜过期误召回=否｜泄漏=否｜命中=['mine']
- `db_hybrid`｜as_of=-｜recall=1.0｜precision=1.0｜token=11｜过期误召回=否｜泄漏=否｜命中=['mine']

### mem_semantic_paraphrase（OK）
- 说明：语义：问句与正文没有共同词面，只有混合检索能召回
- `markdown`｜as_of=-｜recall=1.0｜precision=0.5｜token=25｜过期误召回=否｜泄漏=否｜命中=['runtime', 'web_port']
- `db_keyword`｜as_of=-｜recall=0.0｜precision=0.0｜token=0｜过期误召回=否｜泄漏=否｜命中=[]
- `db_hybrid`｜as_of=-｜recall=1.0｜precision=0.5｜token=25｜过期误召回=否｜泄漏=否｜命中=['runtime', 'web_port']

