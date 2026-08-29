# 新建 Klonet 平台真实测试：待解决问题清单

日期：2026-08-28
测试目标：让 Ops-Privilege Agent 创建一个新的 Klonet 平台，并观察从目标理解、Discovery、Plan/Replan、Binding、执行到验证的完整流程。

## 当前结论

系统已经能够识别“新建平台”是变更目标，并能够进入 Discovery、Replan 和 Binding。但真实测试仍未完成一次端到端创建：Binding 阶段的模型请求超时，安全门终止了执行，因此服务器环境没有发生变化。

下面只记录尚未解决的问题。已经修复的 Planner 必填字段、源码模板健康状态误判完成、Gemini 空 enum 合同错误，不再列入待办。

---

## 1. Binding 超时后恢复粒度过大

### 状态

已完成（2026-08-29）。

- `ChangePlan.binding_cursor` 与 draft `ImplementationPlan` 是唯一 Binding 恢复状态；每次分解、每个原子步骤绑定前后都通过现有 PlanStore checkpoint 持久化。
- provider timeout/connection/rate-limit 等临时异常保留准确语义步骤和原子索引，环境未改变时直接从断点恢复；Coordinator 不重新运行完整 Discovery/Planner。
- 已有 execution binding 会被跳过，恢复不会重新分解语义步骤、重复调用已成功的模型绑定或生成重复 Action；全部绑定完成前计划保持 `draft`、清空授权 hash，不能审批/执行。
- 恢复计划只在持久化 plan goal 与当前 base goal 完全一致时复用；用户改变目标后旧断点不会进入恢复分支。
- Binding 上下文现在携带统一 EvidenceGap resolution。恢复前刷新若把冻结事实（例如端口可用）变为 contradicted，Binder 会在任何新绑定前停止，并按对应 PlanResource consumers 只返回受影响的 semantic step 给 scoped Replan；不相关源码准备步骤保持不变。

验证：`python -m pytest -q tests/test_privileged_workflow_binding.py::test_binding_contradicted_fact_replans_only_the_resource_consumer_step tests/test_privileged_workflow_binding.py::test_binding_timeout_checkpoints_and_resumes_only_unbound_atomic_step tests/test_privileged_workflow_binding.py::test_execution_agent_resumes_draft_implementation_without_redecomposing tests/test_privileged_workflow_coordinator.py tests/test_privileged_mutation_workflow.py::test_invalid_response_text_cannot_replace_deterministic_failure_facts`，结果 `83 passed`。覆盖超时后只绑定未完成原子步骤、断点持久化、禁止部分计划审批、Coordinator 原地恢复、重复恢复无重复 Action、端口事实变化只重规划其 consumer。

### 现象

Binding 已经开始为 `create_docker_container` 补全参数时，模型请求超时。用户选择“继续处理”后，系统没有从失败的 Binding 步骤继续，而是重新进入 Discovery 和 Planner。

### 直观根因

系统只保存了“整个目标失败”，没有把“已经形成的语义计划、当前 Binding 步骤和失败调用”作为可恢复断点。

### 正确行为

如果环境没有改变，并且语义计划仍然有效，应保留原计划和已有证据，只重试或重建失败的 Binding 原子步骤。只有计划合同已经失效时才回到 Planner。

### 解决方案设计

不新增 Agent、并行状态机或第二套计划模型。继续使用现有唯一主链：

```text
Planner
→ 持久化 ChangePlan
→ Binding 逐步绑定并更新同一份 ChangePlan
→ 记录失败断点
→ 从原阶段恢复
→ Binding 完成
→ 生成精确计划哈希
→ 用户审批
→ Executor
→ Verifier
```

#### 1. 在现有 ChangePlan 中保存 Binding 断点

语义计划形成后立即持久化，并在同一份计划中记录：

```text
binding_cursor:
  semantic_step_id: provision-containers
  atomic_step_index: 2
  phase: binding

bound_steps:
  - 已成功绑定的原子步骤及其合同

current_failure:
  step: Create RabbitMQ container
  category: api_timeout
  environment_changed: false
```

这里的 `binding_cursor` 只是现有计划的恢复游标，不是新的完成状态模型。目标是否完成仍只由现有 GoalOutcome/Verifier 判定。

#### 2. Binding 每完成一个原子步骤就更新 PlanStore

```text
绑定 MySQL 成功 → 保存同一份计划
绑定 Redis 成功 → 保存同一份计划
绑定 RabbitMQ 超时 → 保存失败断点
```

已经成功绑定的步骤不能因为后续步骤失败而丢失，也不能在恢复时无理由重新调用模型。部分绑定计划仍属于内部草稿，不能生成审批哈希、不能执行；只有全部 Binding 完成后才形成可审批的精确计划。

#### 3. Coordinator 根据真实失败类型选择恢复阶段

```text
API 超时或临时网络错误
→ 原地重试当前 Binding 步骤

当前 Binding 步骤缺少运行证据
→ Discovery 只补该步骤的 evidence gap
→ 返回当前 Binding 步骤

冻结端口、路径或名称已经失效
→ 只 Replan 受影响的语义步骤

Binding 证明语义计划本身无法实施
→ 回到 Planner

用户修改目标或范围
→ 放弃旧断点，按新目标重新 Planner
```

因此，本次 `api_timeout + environment_changed=false` 应直接恢复失败的容器 Binding，不能重新执行完整 Discovery 和 Planner。

#### 4. 恢复前校验断点依赖的易变事实

只刷新当前步骤实际依赖的事实，例如：

- 冻结端口是否仍空闲；
- 容器名是否仍未占用；
- 目标目录是否仍保持预期状态；
- Screen 会话名是否仍未占用；
- 用户目标和排除项是否发生变化。

事实未变则原地继续；事实变化则只废弃对应消费者步骤。不得因为一个端口变化而推翻不相关的源码准备、配置或其他容器步骤。

#### 5. FailureRecord 只引用权威断点

FailureRecord 保存 `plan_id、失败阶段、semantic_step_id、atomic_step_index、失败类别、environment_changed`，作为用户可读的失败记录和恢复入口；它不复制或创造第二份计划内容。真正的语义步骤、资源和 Binding 结果仍以 PlanStore 中的 ChangePlan 为权威来源。

### 验收

- 在某个 Binding 子步骤制造一次 API 超时。
- 选择继续处理后，不重新运行完整 Planner。
- 已完成或未受影响的语义步骤保持不变。
- 只重试失败的 Binding 步骤。
- 已绑定步骤不再次调用模型，也不产生重复 Action。
- 恢复期间端口被占用时，只重建该端口的消费者步骤。
- 用户修改目标后不得继续使用旧断点。
- 部分绑定结果不得进入审批或执行。
- 多次恢复不能生成重复容器或重复 Screen 动作。

---

## 2. 只读 Shell 兜底无法解释为什么要扩大查询范围

### 状态

已完成（2026-08-29）。

- Discovery 在模型查询前确定性提取用户明确标注的源码目录和目标目录，写入带 confirmed observation 的 `user_decision` 证据；源码路径随即以 exact path subject 执行 bounded `project_layout` Probe。
- `project_layout` 已从通用环境采集器的整机收集路径中拆出为现有 collector 的窄接口，只返回指定项目根布局，不再顺带读取 Nginx、MySQL、Redis、RabbitMQ 等无关主机配置。
- 用户目标中的“不配置/不使用 Nginx”等排除项会进入 EvidenceGap exclusions，Shell fallback 继承并执行相关性校验。
- frozen path subject 不允许 Shell 偷换或扩大；用户未提供 subject 时允许搜索，但凡命令引入绝对路径范围，必须提供 `scope_expansion_reason`，进度界面会显示该原因。
- Planner 的 ready 合同新增 labeled path/role 一致性不变量：用户指定的源码目录必须成为同值 `source_directory`，目标目录必须成为同值 `instance_root`；仅把这些路径以无关角色冻结不能通过。

验证：`python -m pytest -q tests/test_privileged_workflow_mutation.py::test_deployment_contract_binds_labeled_user_paths_to_their_exact_roles tests/test_privileged_workflow_mutation.py::test_sync_directory_deployment_does_not_require_git_resources_or_repository_consumer tests/test_privileged_workflow_discovery.py`，结果 `43 passed`。覆盖显式路径先冻结、精准源码检查、不读取 Nginx、frozen subject 禁止全盘扫描、未知路径扩大范围必须解释、排除项继承和正确 PlanResource 角色绑定。

### 现象

目标已经明确源码模板目录是 `/home/lzl/test/vemu_uestc`，Shell 兜底仍执行：

```bash
find /home /root /opt /var -maxdepth 5 -name master_main.py
```

目标明确排除了 Nginx，Shell 兜底仍读取了 Nginx 配置。

### 直观根因

问题不是 Shell 权限过大，也不是这些目录永远不能查询。用户不知道源码位置、并明确要求 Agent 帮忙寻找时，扩大搜索范围是合理的。

本次错误在于，用户已经明确给出源码模板 `/home/lzl/test/vemu_uestc`，系统仍把任务理解成“寻找服务器上的 Klonet 源码”。调用链中丢失了三类信息：

1. 用户已经提供的源码路径没有直接冻结为权威事实；
2. Evidence gap 只表达“缺少 project layout”，没有携带具体查询对象；
3. Shell Policy 只验证命令是否只读，没有验证命令是否直接解决当前 gap。

因此 Shell 生成器收到的是：

```text
需要 source_directory、template_entry_files
```

而不是：

```text
检查 path=/home/lzl/test/vemu_uestc
确认该目录是否包含创建平台所需的入口文件
```

它只能按通用运维经验扫描常见目录。读取 Nginx 配置也是同类错误：模型从通用 Klonet 部署知识中推断 Nginx 可能提供端口参考，却没有证明它能解决当前的源码布局或端口可用性缺口。

### 正确行为

不设置机械的全局目录黑名单。系统必须确保每一次查询范围都能由当前 EvidenceGap 解释：

```text
用户已经提供的事实
→ 直接冻结，不再重复 Discovery

需要发现的事实
→ EvidenceGap 同时携带查询对象、required facts 和目标边界

注册 Probe
→ 使用同一查询对象执行

注册 Probe 不足
→ Shell 继承同一查询对象和 required facts

Shell 生成完成
→ 校验命令是否直接解决当前 gap
```

本次源码路径应首先形成权威资源：

```text
source_directory=/home/lzl/test/vemu_uestc
status=frozen
source=user_decision
```

随后只需要检查这个目录是否存在，以及所需入口文件是否齐全，不能再次寻找 `source_directory`。

如果用户确实没有提供路径，并且目标是“寻找源码或平台”，可以逐级扩大范围：

```text
已知候选目录
→ 当前用户目录
→ 常用部署目录
→ 更大范围搜索
```

每次扩大范围都必须记录原因，并证明较小范围没有解决当前 gap。显式排除的组件也不是绝对禁止读取；只有当系统能说明其证据与当前 gap 的直接关系时才允许查询。本次 Nginx 与所需事实无关，因此不应读取。

### 解决方案设计

#### 1. 用户明确提供的值优先冻结

Planner 在请求 Discovery 前，先从现有目标资源中判断 required fact 是否已经由用户决定。已冻结的路径、名称和边界不能被重新表述成“待发现事实”。

#### 2. EvidenceGap 必须包含明确查询合同

补证请求至少包含：

```text
gap_id
subject：要检查的路径、实例、端口或资源
required_facts：需要证明的具体事实
scope：允许从哪些对象取得证据
exclusions：当前目标明确排除的范围
```

例如：

```text
subject=/home/lzl/test/vemu_uestc
required_facts=[
  path_exists,
  master_entry_exists,
  worker_entry_exists,
  celery_entry_exists,
  web_terminal_entry_exists
]
```

#### 3. Probe 与 Shell 使用同一份 EvidenceGap

注册 Probe 和 Shell fallback 不是两套补证目标。Shell 只能替换 Probe 的实现方式，不能扩大或改写其 subject、required facts 和排除边界。

#### 4. 执行前增加证据相关性校验

只读安全校验继续负责判断命令是否会修改环境；同时增加基于现有 EvidenceGap 的相关性校验：

- 命令查询对象能否追溯到用户目标或已确认事实；
- 输出是否可能直接证明至少一个 required fact；
- 是否无理由扩大到新的目录、实例或组件；
- 是否查询了与当前 gap 无关的排除组件。

相关性不足时，不直接执行。系统应先缩小命令，或者在确实需要扩大搜索边界时向用户解释原因。

#### 5. 结果必须映射回 required facts

命令有输出不等于补证成功。EvidenceRecord 必须说明本次输出证明、否定或仍未解决了哪些 required facts；无法映射的输出不能进入 Replan 的有效证据集合。

### 验收

- 给出明确源码路径时，该路径直接成为 frozen source resource，不再请求寻找 `source_directory`。
- 对已知模板的入口检查应以该路径为 subject，不能无理由扫描 `/home`、`/root`、`/opt`、`/var`。
- 明确排除 Nginx 且当前 gap 与 Nginx 无关时，不读取 Nginx 配置。
- 用户未提供路径并要求寻找源码时，允许逐级扩大搜索，但日志必须说明扩大原因。
- 每条 Shell 证据都能映射到至少一个 required fact。
- 无关命令即使只读且执行成功，也不能被认定为有效补证。

---

## 3. 失败说明残缺，并且失败阶段上报错误

### 状态

已完成（2026-08-29）。

- ChangeBinder 将 provider timeout/connection/API 异常在 Binding 边界转换为带类别、断点和 exception type 的 `ChangeBindingError`；Coordinator 还有最终异常边界，所有此类失败统一记录为 `stage=binding`，不会被外层误包成 planning。
- FailureRecord 确定性保存 `stage/category/summary/technical_reason/environment_changed/semantic_step_id/atomic_step_index`。
- 用户失败消息始终先渲染确定性事实；Response 模型文本只能作为附加“通俗说明”，不能覆盖失败阶段、技术原因或断点。
- Response 的 `false/true/null/none`、过短、超长或控制流程式无效结果会被拒绝；模型超时/无效时仍输出完整模板。

验证已包含在第 1 项的 `83 passed` 套件中；其中 `test_invalid_response_text_cannot_replace_deterministic_failure_facts` 直接验证 `失败阶段：binding`、API timeout 原因、语义步骤、原子索引且不出现 `失败说明：false`，既有 `test_second_binder_failure_is_persisted_as_blocked_without_traceback` 验证第二次 Binding 失败仍保持真实阶段并安全持久化。

### 现象

用户看到过以下失败说明：

```text
失败说明：false
失败说明：: 无 * 失败校验: 无
失败说明：本次变更
```

实际发生在 Binding 中的 API 超时被上报成了 `planning` 失败。

### 直观根因

底层异常先被转换成了过于笼统的 FailureRecord，随后 Response 又使用模型整理不完整字段，最终丢失了原始阶段和关键原因。

### 正确行为

FailureRecord 必须确定性保存真实阶段、异常类别、当前步骤和环境是否变化。Response 只能改善表达，不能覆盖或截断这些权威字段；模型整理失败时必须回退到确定性模板。

### 验收

- Binding 超时必须显示 `失败阶段：binding`。
- 用户必须看到“哪个步骤超时、是否执行过、环境是否改变”。
- Response 模型无效或超时时，仍能输出完整、可读的确定性说明。

---

## 4. 注册 Probe 与 Planner 的 required facts 没有统一合同

### 状态

已完成（2026-08-29）。现有主链已统一为 `Planner/Verifier → ProbeRequest → EvidenceRecord.observations → EvidenceGap resolution`：

- `required_facts` 在运行时统一为 `FactRequirement`，使用稳定 `fact_id`、typed predicate、expected/comparison 和 freshness；序列化只输出结构化对象。
- 注册 Probe 声明 `supported_predicates`，确定性 extractor 将输出映射到所覆盖的 `fact_id`；原始文本本身不能解决 gap。
- `EvidenceRecord` 同时保留原始输出和可追溯 observation；resolution 仅按 fact identity 计算 confirmed/contradicted/unresolved。
- Shell fallback 必须继承同一 gap、subject 和 covers，并提供确定性 extractor。
- Planner、Discovery、EvidenceSynthesizer、OperationalContext、Goal Verifier 和 Step Verifier 已全部使用同一协议；审计时发现并删除了 Verifier 将 fact 字典转回字符串的残留路径。
- 旧持久化字符串只允许在反序列化入口一次性归一化；运行时和重新持久化的数据不再保留字符串协议。
- Coordinator、Discovery 与 Planner 原先各自解析“源码目录/目标目录”的三份正则已合并到合同层的唯一入口；“源码模板固定使用”“从某目录复制”等表达会形成同一份用户冻结资源，不会在 Discovery 前被误报为缺少源码边界。
- 生产 Probe runner 的展示标题与原始 JSON 已在 Evidence extractor 边界分离；`project_layout` 不会因外层 `recovery_probe` 标题而把存在的目录判为不存在。模型产生的不规范 fact id 只在合同入口规范化一次，之后运行态仍严格使用唯一 canonical fact id。
- 用户冻结的源码根会被 Discovery 显式交给现有 Probe 路径安全边界；该边界仍拒绝其他由模型临时提出的目录。模型给出不受支持的 comparison 时不会放宽或猜测语义，而是在同一个 Discovery 合同内有限校正，二次无效才确定性 blocked，不再让 `ValueError` 穿透主循环。
- 注册 Probe 的 predicate 在执行前与能力目录核对；同义但非标准的 predicate 会在现有 Discovery 内校正一次，确属长尾才改走 Shell。`git_repository、disk、path_permissions、screen` 等真实规划中使用的注册能力已补齐确定性 extractor；复制当前工作树时 Discovery 被明确禁止索取无关 Git remote/branch/revision 事实。
- 注册 Probe 的参数形状也在同一合同入口规范化：模型给出的逗号字符串或单个 `ports/paths/project_roots` 会成为标准列表，`contains_all.expected` 同步成为可比较集合；Probe 不会再因拿到空列表而“成功执行但没有检查目标”。语义含混的布尔入口文件 predicate 被删除，统一使用 `project.entry_files + contains_all`。

验证：结构化证据相关回归与相邻 Coordinator 合同最新结果为 `154 passed`；修改范围结果为 `565 passed`；全量结果为 `1433 passed, 2 failed`。两项失败是修改前即可独立复现的知识检索排序基线（`platform_usage.md` 与 topology node snippet 排名），与本次 Ops-Privilege 链路无关。覆盖注册 ports Probe 无 Shell 重查、生产包装后的 project layout 解析、用户冻结路径可查而相邻路径仍被拒绝、模型 fact id 单次规范化、comparison/predicate/参数形状有限校正、常用注册能力的确定性 extractor、部分 fact 只保留未回答项、原始自然语言不能冒充 observation、frozen path 的 Shell 不得换 subject、Verifier 完整保留结构化 gap 合同等场景。

真实 CLI 复测中，显式源码 `/home/lzl/test/vemu_uestc` 由一次注册 `project_layout` 直接确认，目标路径与端口 Probe 在参数规范化后直接产生 confirmed/contradicted observations，均未再触发 Shell。流程最终停在外部 Change Planner API timeout；失败被正确归属为 `planning`、环境未改变。该外部规划可用性属于第 7 项端到端验证的剩余阻塞，不影响本节证据协议验收。

### 现象

Planner 请求：

```text
ports（ports_availability）
project_layout（source_directory、template_entry_files）
```

注册 Probe 已成功执行，系统却又判断“未覆盖所需事实”，随后转入 Shell 兜底。

### 直观根因

Planner 使用自由文本描述 required facts，而 Probe 输出另一套字段。系统没有权威映射来判断某个 Probe 究竟能证明哪些事实。

### 正确行为

每个注册 Probe 应声明可提供的事实类型和结构化输出。Planner 请求的事实必须能与这些事实类型匹配；无法匹配时再进入 Shell 兜底。

### 解决方案设计：统一证据交接协议

不增加“证据解释 Agent”或并行证据模型。直接收敛现有调用链：

```text
Planner
→ EvidenceGap
→ ProbeRequest
→ EvidenceRecord
→ EvidenceGap resolution
→ Replan
```

现有 `ProbeRequest.required_facts` 的自由文本形式应被结构化要求替代；现有 `EvidenceRecord` 在保留原始输出的同时，必须明确回应这些要求。迁移时应同步更新所有调用方并删除旧字符串协议，不能长期双轨兼容。

#### 1. EvidenceGap：当前缺口的唯一权威定义

```json
{
  "gap_id": "gap-project-layout-01",
  "affected_steps": ["prepare-source"],
  "subject": {
    "kind": "path",
    "value": "/home/lzl/test/vemu_uestc"
  },
  "requirements": [
    {
      "fact_id": "fact-source-path-exists",
      "predicate": "path.exists",
      "expected": true,
      "comparison": "equals",
      "freshness": "cached"
    },
    {
      "fact_id": "fact-runtime-entries",
      "predicate": "project.entry_files",
      "expected": [
        "master_main.py",
        "worker_main.py",
        "celery_worker.py",
        "web_terminal_main.py"
      ],
      "comparison": "contains_all",
      "freshness": "cached"
    }
  ]
}
```

字段职责：

- `gap_id`：本轮缺口身份；Replan 只能针对它受影响的步骤。
- `affected_steps`：缺口解决后允许修订的语义步骤。
- `subject`：要检查的对象，不能在 Probe 或 Shell 阶段被偷偷换掉。
- `fact_id`：一次目标内稳定的事实身份，避免依赖自然语言同义词匹配。
- `predicate`：事实语义，供能力匹配和审计使用。
- `expected/comparison`：怎样才算该事实得到证明。
- `freshness`：能否复用缓存证据，或必须刷新运行态。

`predicate` 不设计成覆盖所有未来场景的巨大固定 enum。注册 Probe 声明自己支持的 predicate；长尾 Shell 可以实现新的 predicate，但必须继承原 `fact_id、subject、expected`，并提供可校验的提取合同。

#### 2. ProbeRequest：声明本次检查覆盖哪些 fact_id

```json
{
  "request_id": "probe-project-layout-01",
  "gap_id": "gap-project-layout-01",
  "probe": "project_layout",
  "args": {
    "path": "/home/lzl/test/vemu_uestc"
  },
  "covers": [
    "fact-source-path-exists",
    "fact-runtime-entries"
  ]
}
```

Planner 不再向 Probe 发送 `source_directory`、`template_entry_files` 这类无法核对的自由文本。Discovery 在执行前验证：

- `covers` 中的 fact_id 必须属于当前 gap；
- Probe 声明的能力能够回答对应 predicate；
- `args.path` 等查询对象必须与 gap subject 一致。

#### 3. EvidenceRecord：原始证据与事实结论同时保存

```json
{
  "evidence_id": "ev-project-layout-01",
  "request_id": "probe-project-layout-01",
  "gap_id": "gap-project-layout-01",
  "raw_output": "...原始 Probe 或命令输出...",
  "observations": [
    {
      "fact_id": "fact-source-path-exists",
      "status": "confirmed",
      "value": true,
      "extractor": "project_layout.path_exists"
    },
    {
      "fact_id": "fact-runtime-entries",
      "status": "confirmed",
      "value": [
        "master_main.py",
        "worker_main.py",
        "celery_worker.py",
        "web_terminal_main.py"
      ],
      "extractor": "project_layout.entry_files"
    }
  ]
}
```

事实级 `status` 只表达证据观察：

- `confirmed`：证据支持该事实要求；
- `contradicted`：证据明确否定该事实要求；
- `unresolved`：本次证据无法判断。

这些状态不能替代 GoalOutcome。只有 Verifier 可以判定整个用户目标是否 achieved；某个 fact confirmed 不代表任务完成。

`raw_output` 保留审计依据，`observations` 用于模块通信。Replan 不应再次从大段原始文本猜测字段含义。

#### 4. EvidenceGap resolution：确定性计算缺口变化

```json
{
  "gap_id": "gap-project-layout-01",
  "confirmed_fact_ids": [
    "fact-source-path-exists",
    "fact-runtime-entries"
  ],
  "contradicted_fact_ids": [],
  "unresolved_fact_ids": []
}
```

该结果由 requirements 与 observations 按 `fact_id + comparison` 确定性计算，不由 LLM 总结决定。

Replan 的进展定义收敛为：

```text
本轮前后的 unresolved_fact_ids 集合确实缩小
或某个 unresolved fact 得到 contradicted，形成可行动的新结论
```

仅新增 EvidenceRecord、命令有输出或换了一种自然语言表述，都不算进展。

#### 5. 端口示例

Planner 不再分别请求含义模糊的 `master_port_available`、`worker_port_available`。它创建带角色和值的要求：

```json
{
  "gap_id": "gap-port-allocation-01",
  "subject": {
    "kind": "port_set",
    "value": [47001, 47002, 47009]
  },
  "requirements": [
    {
      "fact_id": "fact-master-port-free",
      "predicate": "port.available",
      "expected": 47001,
      "comparison": "contains"
    },
    {
      "fact_id": "fact-worker-port-free",
      "predicate": "port.available",
      "expected": 47002,
      "comparison": "contains"
    },
    {
      "fact_id": "fact-mysql-port-free",
      "predicate": "port.available",
      "expected": 47009,
      "comparison": "contains"
    }
  ]
}
```

`ports` Probe 返回可用端口集合后，确定性解析器按端口值回应三个 fact_id。角色与端口的对应关系来自 Planner 已冻结的资源消费者关系，不能由 Probe 猜测。

#### 6. Shell fallback 仍使用同一协议

当注册 Probe 不能实现某个 requirement 时，Shell 生成器接收同一份 EvidenceGap，而不是新的自然语言任务。Shell 合同必须声明：

```text
command
covers fact_ids
subject
output extractor
如何从退出码或输出得到每个 fact_id 的 observation
```

Policy 同时验证只读安全和协议一致性。Shell 可以覆盖长尾能力，但不能修改 gap、换 subject 或伪造新的 required facts。

#### 7. 协议不变量

在字段结构之外，还必须固定各模块对协议对象的写权限：

| 对象或字段 | 创建/修改者 | 其他模块的权限 |
|---|---|---|
| `EvidenceGap.subject/requirements/affected_steps` | Planner/Replanner | Discovery、Binder、Verifier 只读；用户改变目标时才允许 Planner 生成新版本 |
| `ProbeRequest.probe/args/covers` | Discovery | 必须引用既有 gap，不能新增、删除或改写 requirement |
| `EvidenceRecord.raw_output` | Probe 或只读 Shell 执行器 | 后续模块只读，禁止 Response 或 LLM 摘要覆盖 |
| `EvidenceRecord.observations` | 与 Probe/Shell 绑定的确定性 extractor | LLM 可以提出校正请求，但不能直接把 `unresolved` 改成 `confirmed` |
| `EvidenceGap resolution` | 统一的确定性比较函数 | Planner、Binder、Verifier 共同读取，不允许各自实现另一套“证据是否足够”判断 |
| 用户冻结的资源和边界 | 用户决策进入现有 `PlanResource` | Discovery 不能把它降级为待发现事实，Planner 修改前必须再次征求用户确认 |
| 整体目标状态 | 现有 `GoalOutcome`/Verifier | fact 或 gap 状态不得直接产生第二套 `achieved/completed/ready` 判据 |

对应的数据流只能单向推进：

```text
Planner 定义缺什么
→ Discovery 选择怎么查
→ Probe/Shell 产生原始事实
→ 确定性 extractor 回答 fact_id
→ 统一 resolution 计算还缺什么
→ Replanner 只修改 affected_steps
```

如果某个 observation 无法由确定性 extractor 产生，正确状态是 `unresolved`，并由 Planner 决定换 Probe、使用 Shell 或请求用户，而不是让 Response、Discovery 或 Verifier 凭自然语言“猜成已确认”。

- 一个 `fact_id` 在同一目标中只能表示一个 subject、predicate 和 expected。
- Probe/Shell 只能回应自己 `covers` 的 fact_id。
- EvidenceRecord 必须保留原始证据，结构化 observation 必须能追溯到确定性 extractor。
- 无法映射到 fact_id 的输出不得交给 Replan 作为有效新事实。
- 用户决策不能伪装成可发现事实；用户已冻结的值直接进入 PlanResource。
- 运行态事实必须遵守 freshness，静态证据不能冒充最新状态。
- fact 状态不能成为第二套目标完成判据。

### 验收

- `ports` 一次返回已检查端口、占用端口和可用端口后，不再重复请求同一事实。
- `project_layout` 针对给定源码路径返回入口文件后，不再全盘搜索。
- 同义的事实名称不会导致重复补证。
- Probe 返回不同字段名或不同语言时，只要回应相同 fact_id，结果保持一致。
- 部分 fact 得到确认时，只保留真正未解决的 fact_id，不重复查询已确认部分。
- Shell 输出无法映射到 covers fact_ids 时，不得被认定为有效进展。
- 同一 EvidenceGap 的解析结果不依赖 LLM 是否正确理解原始输出文本。
- fact confirmed 不得直接导致用户目标 achieved。

---

## 5. Replan 把无关命令输出也当成“取得新证据”

### 状态

已完成（2026-08-29）。Replan 进展不再依据 EvidenceRecord 数量或原始输出 hash，而是仅比较当前 gap 的 `(unresolved_fact_ids, contradicted_fact_ids)`：

- 无法映射到 active gap fact_id 的 Shell/Probe 输出不改变进展状态。
- 相同 observation 的重复结果不算新进展。
- fact 从 unresolved 变为 confirmed 或 contradicted 才形成可行动进展。
- 成功进展日志现在确定性显示 `gap=<id> 已解决=<fact ids> 仍未解决=<fact ids>`，不再只输出泛化的“取得新事实”。

验证：新增 `test_replan_progress_ignores_unmapped_output_and_reports_fact_delta`，覆盖无关 Nginx 文本、有效 fact observation、重复 observation 和 gap/fact 日志；`python -m pytest -q tests/test_privileged_workflow_coordinator.py` 结果 `79 passed`。

### 现象

Shell 读取了与目标无关的 Nginx 配置后，系统仍然输出：

```text
已取得新的目标相关事实，正在重新规划。
```

### 直观根因

当前进展判定更接近“新增了一条 EvidenceRecord”，而不是“当前 evidence gap 的 required facts 被解决了”。

### 正确行为

只有新增证据直接解决或缩小当前 gap 时才算有进展。无关输出、重复事实和无法归属到 required facts 的记录都不应消耗有效 Replan 轮次。

### 验收

- 无关 Shell 输出不能触发“取得新事实”。
- 同一 Probe 的重复结果不算新进展。
- 每次 Replan 日志应显示解决了哪个 gap、还剩哪些事实。

---

## 6. 相同目标生成的计划结构不稳定

### 状态

已完成（2026-08-29）。

- Planner 现在使用稳定的部署阶段词汇识别 `source、dependencies、configuration、runtime、nginx`，同时兼容 Git clone 和“同步/复制当前工作树”等同义表达。
- 依赖关系被确定性编译为“源码同步 → 依赖容器 → 配置 → 应用启动 → Nginx”，再由拓扑排序生成执行顺序；配置不再被遗漏在依赖容器与 Screen 运行时之间。
- 既有 candidate plan 仍是 Replan 的权威前身；`active_gap_affected_steps` 之外的步骤若被删除或发生语义变化，Planner 会拒绝该轮 Replan。

验证：新增完整部署阶段乱序归一化用例，并扩展现有语义依赖测试覆盖 `sync_directory` 和独立配置阶段；连同 Replan 非活动步骤保护用例运行结果为 `3 passed`。完整 `tests/test_privileged_workflow_mutation.py` 结果为 `170 passed`。

### 现象

同一个创建目标在不同轮次中生成过 3 个或 4 个语义步骤，请求的 Probe 集合和步骤顺序也不同。

### 直观根因

用户目标虽然固定，但 Planner 对“源码准备、依赖容器、配置、应用启动”这些 Klonet 部署阶段缺少稳定的语义分解约束，导致模型每轮重新解释整个目标。

### 正确行为

同一目标和同一证据应得到语义等价的阶段结构。Replan 只能修订当前证据缺口影响的步骤，不能无理由重写其他阶段。

### 验收

- 相同目标和证据连续规划多次，核心语义步骤集合一致。
- 步骤顺序始终满足：源码准备 → 依赖准备 → 配置 → 应用启动 → 验证。
- Replan 不修改 active gap 之外的步骤。

---

## 7. 尚未完成一次端到端的新平台创建验证

### 现象

当前只证明了流程能够走到 Binding。由于 API 超时，还没有形成并执行一份完整、安全的最终计划。

### 需要证明的完整合同

- 使用 `sync_directory` 复制指定工作树，而不是 Git clone。
- 只创建目标实例的 MySQL、Redis、RabbitMQ 容器。
- 正确冻结应用端口、容器宿主端口和内部端口。
- 正确设置全部必需的 Klonet 配置属性。
- 创建 master、celery、web_terminal、worker 四个指定 Screen。
- 不启动 data_server，不修改 Nginx，不影响其他平台。
- Executor 执行后由 Verifier 独立证明容器、配置、Screen、端口和健康状态。

### 验收

必须在一个全新实例名和全新目录上完成真实执行，并使用独立运行态证据核验；不能仅依赖计划或执行器声称成功。

---

## 8. 模糊创建请求过早执行大量 Discovery

### 状态

已完成（2026-08-29）。Coordinator 在进入 Discovery 前先执行部署边界门禁：平台标识、源码位置和目标根目录缺失时直接返回用户决策问题，不运行 runtime、Git、Nginx 或端口 Probe；目标目录可同时提供唯一实例身份时不重复询问平台名，用户授权自动选择的端口不作为用户问题。

验证：新增模糊创建请求不进入 Discovery、边界齐全后正常进入规划的测试；`tests/test_privileged_workflow_coordinator.py` 相关套件通过。

### 现象

用户只说“创建一个新的 Klonet 平台”时，系统先查询运行时、服务健康、端口、Python、项目布局、Git 和 Nginx，最后才询问平台名、源码来源和目标目录。

### 直观根因

系统没有先区分“必须由用户决定的边界”和“可以由 Discovery 获取的事实”。

### 正确行为

先询问不可推导的用户决策；用户给出平台标识、目标路径和源码来源后，再针对这些边界进行 Discovery。

### 验收

- 对模糊创建请求，第一轮直接询问必要边界。
- 在边界确定前，不扫描无关运行时、Git 或 Nginx 状态。
- 用户明确授权自动选择的端口不应再次作为用户问题。

---

## 9. Shell 原始输出直接淹没用户界面

### 状态

已完成（2026-08-29）。Discovery 默认进度只展示 Probe 目的、退出状态、证据 ID、fact 摘要和范围扩张原因，原始 stdout/stderr 仅保存在 EvidenceRecord；用户通过 `show-priv-evidence <evidence-id>` 显式查看时才返回脱敏后的技术详情。

验证：新增默认输出不包含 `Permission denied`/长原文、显式详情命令返回脱敏内容的测试；Discovery 与 Coordinator 相关套件共 `124 passed`。

### 现象

`find` 和 `ss` 的完整输出、测试目录以及大量 `Permission denied` 被直接打印到对话中。

### 直观根因

执行日志和用户进度输出没有分层；Evidence 原文被当成了用户可读进度。

### 正确行为

原始输出保存到 EvidenceRecord，用户界面只显示命令目的、退出状态和事实摘要。技术详情应通过显式详情命令查看。

### 验收

- 默认界面不打印长命令输出和权限错误列表。
- 用户仍可通过技术详情查看脱敏后的原始证据。
- 摘要明确说明查到了什么，而不是只显示“执行完成”。

---

## 10. 启动时出现 Jieba 缓存权限 Traceback

### 状态

已完成（2026-08-29）。`MixedTokenizer` 在调用 `jieba.Tokenizer.add_word()` 触发初始化前，先把缓存目录设置为当前用户的 `${XDG_CACHE_HOME:-~/.cache}/klonet_agent/jieba`；目录不可用时回退到当前进程创建的私有临时目录，不再复用共享 `/tmp/jieba.cache`。

验证：

- 新增 `test_jieba_cache_is_scoped_to_a_writable_user_directory`，先确认旧实现因缺少独立缓存配置而失败，再确认修复后通过。
- 与此前稳定复现 Traceback 的知识检索用例一同运行时，Jieba 权限 Traceback 已消失。
- 验证命令：`python -m pytest -q tests/test_knowledge.py::test_jieba_cache_is_scoped_to_a_writable_user_directory tests/test_intent_routing.py::test_platform_usage_query_retrieves_indexed_user_guide_evidence tests/test_intent_routing.py::test_topology_node_type_query_retrieves_curated_node_guide`；结果为缓存测试通过，另外两个既有知识检索排序断言失败（与缓存修复无关，未改 knowledge 索引或排序逻辑）。

### 现象

每次启动 Agent 都会打印：

```text
Dump cache file failed.
PermissionError: ... /tmp/jieba.cache
```

程序随后可以继续运行。

### 直观根因

多个用户或进程共享固定的 `/tmp/jieba.cache`，当前进程无法安全替换该缓存文件。

### 正确行为

缓存应放入当前用户可写的独立目录；缓存不可写时也不应向普通用户打印完整 Traceback。

### 验收

- 连续启动和多用户启动均不出现缓存权限异常。
- Agent 的功能不依赖全局共享的临时缓存文件。

---

## 11. CLI Token 统计始终为 0

### 状态

已完成（2026-08-29）。

- `LLMClient` 在统一调用边界读取 OpenAI/Gemini 兼容的 total 或 input/output usage；流式请求显式请求 usage，并在流消费结束后只累计最终统计一次。
- Orchestrator 聚合普通回答、意图分析、Planner、Binder、Verifier、Response、Shell 生成等所有独立客户端，不再只看主回答客户端。
- provider 未返回 usage、流中断或最终请求失败时记录为 unavailable，CLI 显示“本次累计 token 统计不可用”，不再把缺失数据伪装成 0；只要存在未统计调用，就不展示不完整累计数。
- 重试策略：没有返回 provider response 的失败尝试不猜测 token，也不逐次累加；一次逻辑请求最终成功时仅累计成功响应一次，全部重试失败时将该逻辑调用记为 unavailable。

验证：新增非流式/流式 usage 汇总、provider 缺失 usage、CLI 可用/不可用渲染、多客户端去重聚合测试；`tests/test_llm_client.py tests/test_cli_entry.py tests/test_orchestrator_controls.py` 及 Planner/Binder/Verifier/Response/Coordinator 回归共 `341 passed`。

### 现象

会话进行了多次模型调用，退出时仍显示：

```text
本次累计 token 约 0
```

### 直观根因

统一模型调用层没有把 provider usage 汇总并回传到 CLI 的 token 计数。

### 正确行为

所有 Planner、Binder、Verifier、Response 和 Shell 生成调用都应进入同一 usage 汇总；provider 不返回 usage 时，应明确显示“不可用”，而不是错误显示 0。

### 验收

- 有 usage 的模型调用后，CLI 显示非零累计值。
- provider 不提供 usage 时显示“统计不可用”。
- 重试和失败调用的统计策略一致且有文档说明。

---

## 12. 新平台目标在失败记录中显示“目标未确认”

### 状态

已完成（2026-08-29）。目标身份仍使用同一实例生命周期，不增加第二套模型：

- 计划存在时，诊断持久化从 `instance_identifier` 和 `instance_root` 类 PlanResource 读取身份。
- 新实例尚未形成计划或尚未进入 RuntimeInventory 时，从已确认 `user_decision.target_directory` 和权威 goal 中明确标注的平台名读取同一身份。
- 创建完成后，原有 RuntimeInventory 继续以相同 project root/identifier 表示运行实例；失败恢复仍以原 goal 与 PlanResource 为权威，不会因“未运行”丢失目标。

新增 `test_failed_new_deployment_persists_the_user_frozen_target_identity`，模拟无 plan、Binding 超时的新部署失败，验证共享诊断记录包含 `target: create_agent_e2e, /home/lzl/create_agent_e2e/vemu_uestc` 且不再出现 `target: 未确认`。`python -m pytest -q tests/test_privileged_integration.py` 结果 `12 passed`。

### 现象

用户已经明确给出：

```text
平台名 create_agent_e2e
目标目录 /home/lzl/create_agent_e2e/vemu_uestc
```

但持久化诊断记录仍显示：

```text
target: 未确认
```

### 直观根因

目标身份解析主要依赖 RuntimeInventory。新平台尚未存在，因此无法从运行实例清单中解析身份；用户已经冻结的部署目标没有成为同一目标身份模型的权威输入。

### 正确行为

目标身份模型应同时支持“已存在运行实例”和“尚未创建但已由用户冻结的部署实例”。这两种状态属于同一个实例生命周期，而不是两套身份模型。

### 验收

- 新实例尚不存在时，FailureRecord 仍能显示用户冻结的平台名和目标目录。
- 创建完成后，同一身份自然转为 RuntimeInventory 中的已存在实例。
- 恢复流程不会因为目标尚未运行而丢失部署目标。

---

## 建议评审顺序

1. 注册 Probe 与 required facts 的统一合同。
2. Replan 的真实进展判定。
3. Shell 兜底的目标范围和输出治理。
4. Binding 超时后的原位置恢复。
5. Planner 语义结构稳定性。
6. 完整端到端创建测试。
7. 模糊目标的询问时机。
8. FailureRecord/Response 表达和目标身份。
9. Jieba 缓存与 Token 统计等运行体验问题。

前三项应作为一个连续问题处理：先明确缺什么事实，再限制如何取证，最后只把真正解决缺口的证据交回 Replan。
