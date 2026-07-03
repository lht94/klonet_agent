"""工具注册表。

这里保存给大模型看的工具 schema，例如 run_command、load_skill、search_knowledge 等。
它只负责声明工具能力，不负责真正执行工具。
"""

from __future__ import annotations


# 工具数组定义。LLM 会通过这段 JSON schema 判断什么时候需要调用什么工具，
# 并输出调用该工具的标准参数。
# 输出依据是 function.parameters；是否调用的依据是 function.description。
def _tool(name: str, description: str, properties: dict, required: list[str]):
    """减少工具 schema 的重复样板。"""

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOLS = [
    {
        # 工具的类型：function 表示这是一个函数工具。
        "type": "function",
        "function": {
            # 工具名，后续 ToolExecutor 会根据这个名字分发到同名能力。
            "name": "run_command",
            # 工具说明，LLM 通过它判断什么时候调用工具。
            "description": "在终端执行shell命令并返回输出",
            "parameters": {
                # 参数类型：object 对应 Python 里的字典。
                "type": "object",
                "properties": {
                    # 参数设计得越规范，模型输出越稳定，不容易格式错误。
                    "command": {
                        "type": "string",
                        "description": "要执行的shell命令，例如 'ls -la' 或 'mkdir mydir'",
                    }
                },
                # required 表示模型调用该工具时必须提供 command 字段。
                "required": ["command"],
            },
        },
    },
    _tool(
        "search_knowledge",
        "仅检索 Klonet 专属知识。明确不需要 Klonet 的通用技术问题不要调用；Klonet 域内问题和 Coding 规范查询再使用。",
        {
            "query": {"type": "string", "description": "检索问题或关键词"},
            "intent": {
                "type": "object",
                "description": "对原始用户需求的结构化理解；必须保留否定、前置条件和纠正信息",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["klonet", "general", "mixed"],
                    },
                    "task_type": {
                        "type": "string",
                        "enum": [
                            "concept",
                            "deployment_preparation",
                            "deployment_guidance",
                            "credential_boundary",
                            "operation_guide",
                            "troubleshooting",
                            "code_lookup",
                            "development",
                            "project_progress",
                            "general",
                        ],
                    },
                    "operation": {
                        "type": "string",
                        "enum": [
                            "unknown",
                            "environment_setup",
                            "dependency_install",
                            "platform_start",
                            "platform_stop",
                            "platform_restart",
                            "topology_deploy",
                            "acceptance_check",
                        ],
                    },
                    "target": {"type": "string"},
                    "symptom": {"type": "string"},
                    "excluded_intents": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "prerequisites": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "requires_retrieval": {"type": "boolean"},
                    "requires_environment_diagnosis": {"type": "boolean"},
                    "clarification_required": {"type": "boolean"},
                    "clarification_question": {"type": "string"},
                    "is_correction": {"type": "boolean"},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
                "required": ["scope", "task_type", "operation", "confidence"],
            },
            "top_k": {"type": "integer", "description": "返回条数，默认 3"},
            "task_type": {
                "type": "string",
                "enum": [
                    "concept",
                    "deployment_preparation",
                    "deployment_guidance",
                    "credential_boundary",
                    "operation_guide",
                    "troubleshooting",
                    "code_lookup",
                    "development",
                    "project_progress",
                    "general",
                ],
                "description": "可选任务类型，用于选择知识层权重",
            },
            "layers": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["curated", "experience", "machine_index", "local"],
                },
                "description": "可选知识层过滤",
            },
            "domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选业务域过滤，例如 topology、vm、traffic",
            },
            "min_priority": {
                "type": "string",
                "enum": ["P0", "P1", "P2", "P3"],
                "description": "可选最低知识优先级",
            },
        },
        ["query", "intent"],
    ),
    _tool(
        "render_klonet_config",
        "只读生成 Klonet 新平台部署配置草案，不写文件。根据平台名、服务器名、master/public/web_terminal 端口、前端 alias 和前端目录，输出 nginx server block 与常见 frontend config.js 草案；写入必须再通过 OperationPlan 绑定 write_ops_file，生效必须再绑定 reload_nginx。",
        {
            "platform": {"type": "string", "description": "新平台或目标平台名，例如 103、lht2。"},
            "server_name": {"type": "string", "description": "Nginx server_name 或前端访问主机/IP，例如 192.168.1.33。"},
            "master_port": {"type": "integer", "description": "Master/Gunicorn 后端端口。"},
            "public_port": {"type": "integer", "description": "Nginx 对外监听端口，也就是浏览器访问的 public_port。"},
            "terminal_port": {"type": "integer", "description": "Web Terminal 端口。"},
            "frontend_alias": {"type": "string", "description": "Nginx 前端 location，例如 /VEMU2/ 或 /VEMU2-103/，必须以 / 开始。"},
            "frontend_path": {"type": "string", "description": "前端静态目录绝对路径，例如 /home/adminis/lht/103_project/vemu_frontend/VEMU2。"},
        },
        ["platform", "server_name", "master_port", "public_port", "terminal_port", "frontend_alias", "frontend_path"],
    ),
    _tool(
        "inspect_ops_context",
        "只读收集 Ops 全量环境上下文：baseline 适合半永久记录，runtime 需要按次刷新，assets 只扫描允许目录中的源码/Compose/Dockerfile/部署配置文件名。",
        {
            "sections": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["baseline", "runtime", "assets"],
                },
                "description": "要采集的上下文分区；默认采集 baseline、runtime、assets。",
            },
            "asset_roots": {
                "type": "array",
                "items": {"type": "string"},
                "description": "允许扫描部署资产的目录；默认当前工作目录。只返回文件名，不读取敏感配置正文。",
            },
            "max_assets": {
                "type": "integer",
                "description": "最多返回多少个部署资产文件名，默认 100。",
            },
        },
        [],
    ),
    _tool(
        "inspect_system_environment",
        "只读检查本机基础环境状态，返回 detected/missing/unchecked。用于 Klonet 运维故障诊断，不会修改环境。",
        {
            "checks": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["os", "python", "system_python", "disk", "virtualization"],
                },
                "description": "可选检查项；默认检查 os、python、disk、virtualization。system_python 用于确认 /usr/bin 等系统自带 Python 路径和版本，不读取二进制文件。",
            }
        },
        [],
    ),
    _tool(
        "inspect_platform_instances",
        "只读盘点当前服务器上的 Klonet 平台实例。综合 screen 会话、Klonet 相关进程 cwd/命令和可选项目根目录 config.py，输出 platform、roles、screen_sessions、pids、project_roots、ports；部署/重启/销毁前应先用它检查所有平台名称和端口冲突。",
        {
            "project_roots": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选：已知 Klonet 项目根目录列表，用于读取各自 config.py 中的端口配置，例如 /home/adminis/lht/102_project。",
            },
            "max_instances": {
                "type": "integer",
                "description": "最多返回多少个平台实例，默认 50。",
            },
        },
        [],
    ),
    _tool(
        "inspect_klonet_runtime",
        "只读检查本机 Klonet 相关运行状态，例如端口、screen、nginx、Docker、Redis、RabbitMQ、MySQL、OVS、KVM、libvirt。",
        {
            "checks": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "ports",
                        "port_owner",
                        "screen",
                        "processes",
                        "process_details",
                        "nginx",
                        "docker",
                        "redis",
                        "rabbitmq",
                        "mysql",
                        "ovs",
                        "kvm",
                        "libvirt",
                    ],
                },
                "description": "可选检查项；默认检查常见 Klonet 运行依赖",
            },
            "ports": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "当 checks 包含 port_owner 时，按端口精确查询占用 PID、命令和 cwd，例如 [5045]。",
            },
            "pids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "当 checks 包含 process_details 时，按 PID 查询进程详情。",
            },
            "process_keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "当 checks 包含 process_details 时，按进程命令关键词筛选，例如 web_terminal_main.py。",
            },
        },
        [],
    ),
    _tool(
        "inspect_process_detail",
        "只读精确确认端口、PID 或进程关键词对应的进程证据，返回 port/pid/ppid/user/cmd/cwd。用于 address already in use、确认端口占用者、核对运行源码 cwd；不修改环境。",
        {
            "ports": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "要精确确认占用者的监听端口，例如 [5045]。",
            },
            "pids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "要读取 cmd/cwd/ppid/user 的进程 PID 列表。",
            },
            "process_keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "按进程命令关键词查找 PID 后读取详情，例如 web_terminal_main.py。",
            },
        },
        [],
    ),
    _tool(
        "read_klonet_logs",
        "只读读取安全日志文件尾部并脱敏，返回 resolved_path、mtime、size_bytes 以确认日志来源。旧日志里的历史错误不能单独证明当前仍然故障；需要结合进程、端口或 screen 输出判断当前状态。拒绝 .env、私钥、token、密码和非日志后缀文件。",
        {
            "path": {"type": "string", "description": "日志文件路径，仅允许普通日志后缀"},
            "max_chars": {"type": "integer", "description": "最多返回尾部字符数，默认 8000"},
        },
        ["path"],
    ),
    _tool(
        "read_ops_file",
        "只读读取 Klonet 运维相关配置/源码/部署文件并脱敏，例如 config.py、nginx .conf、Compose、Dockerfile、systemd service、启动脚本和前端 config.js。可用于核对端口、路径、Nginx 路由和启动参数；不能读取 .env、私钥、token 或密码文件，也不能单独替代进程/端口/screen 的运行态证据。",
        {
            "path": {
                "type": "string",
                "description": "要读取的配置/源码/部署文件路径，可为服务器绝对路径；支持常见 .py/.conf/.yml/.yaml/.json/.ini/.service/.sh/.js 等运维文本文件。",
            },
            "max_chars": {"type": "integer", "description": "最多返回尾部字符数，默认 8000"},
        },
        ["path"],
    ),
    _tool(
        "inspect_screen_session",
        "只读抓取指定 screen 会话的窗口/滚屏快照，用于查看 master、worker、celery、web_terminal 最近输出。返回 screen_scrollback 证据，current_state=false；不能单独证明当前进程仍存活。使用 screen hardcopy，不发送交互输入。",
        {
            "session": {
                "type": "string",
                "description": "screen 会话名或 id.name，例如 1024293.102_m 或 102_m",
            },
            "max_chars": {"type": "integer", "description": "最多返回尾部字符数，默认 8000"},
        },
        ["session"],
    ),
    _tool(
        "search_shared_ops_memory",
        "按需检索多用户共享 Ops 记忆中的历史诊断记录。用于查找超过最近几天未自动注入的相似问题；返回内容只能作为历史线索，不能直接当作当前环境事实。",
        {
            "query": {
                "type": "string",
                "description": "要检索的历史问题、平台名、服务名、端口、报错或诊断关键词",
            },
            "max_results": {
                "type": "integer",
                "description": "最多返回多少条日期文件结果，默认 5",
            },
        },
        ["query"],
    ),
    _tool(
        "create_ops_operation_plan",
        "为 deploy_platform、restart_platform 或 destroy_platform 创建受控 Ops 操作计划。只保存计划，不执行任何环境修改；可为步骤绑定白名单 recipe_id 和结构化参数，但必须等用户确认后才能执行。",
        {
            "operation": {
                "type": "string",
                "enum": ["deploy_platform", "restart_platform", "destroy_platform"],
                "description": "计划类型：部署、重启或销毁平台。",
            },
            "target": {
                "type": "string",
                "description": "目标平台名、实例名或待创建平台名；不确定时写 unknown。",
            },
            "objective": {
                "type": "string",
                "description": "用户可读的计划目标。",
            },
            "constraints": {
                "type": "string",
                "description": "来自 ops.planner 或只读工具的约束，例如端口冲突、screen 冲突、Redis/Docker 复用判断。",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "本轮只读工具或知识库证据摘要。",
            },
            "operation_args": {
                "type": "object",
                "description": "可选：计划级结构化参数，例如 deploy_platform 的 {\"project_root\":\"/home/adminis/lht/103_project/vemu_uestc\"}。只保存并用于生成受控默认 recipe，不执行。",
            },
            "recipe_bindings": {
                "type": "object",
                "description": "可选：按 step_id 绑定受控 recipe，例如 restart_screen_component、prepare_project_files、extract_archive、run_install_script、write_ops_file、reload_nginx。extract_archive 参数为 {\"archive_path\":\"/path/pkg.tar\",\"destination_dir\":\"/root\"}；run_install_script 只允许 base_requ_setup.sh NORMAL 或 docker_service.sh，参数为 {\"script_dir\":\"/root/vemu_install_new_gen\",\"script_name\":\"base_requ_setup.sh\",\"script_args\":\"NORMAL\"}；write_ops_file 参数为 {\"path\":\"/path/config.py\",\"content\":\"...\"}，dry-run 时只脱敏预览，真实执行会备份原文件并拒绝 .env、密钥、token、password 等敏感路径；reload_nginx 无参数，通过 helper 固定执行 nginx -t 成功后再 nginx -s reload。只保存绑定，不执行。",
            },
        },
        ["operation", "target"],
    ),
    _tool(
        "list_ops_operation_plans",
        "List recent Ops OperationPlans with status and next_step summaries. This is read-only and never approves or executes steps.",
        {
            "limit": {
                "type": "integer",
                "description": "Maximum number of plans to return, default 10 and capped at 50.",
            },
            "status": {
                "type": "string",
                "enum": ["pending", "approved", "aborted", "completed", "failed"],
                "description": "Optional plan status filter.",
            },
            "operation": {
                "type": "string",
                "enum": ["deploy_platform", "restart_platform", "destroy_platform"],
                "description": "Optional operation type filter.",
            },
            "target": {
                "type": "string",
                "description": "Optional exact target/platform filter.",
            },
        },
        [],
    ),
    _tool(
        "describe_ops_operation_plan",
        "Read and render the current state of an existing Ops OperationPlan. This is read-only and never approves or executes steps.",
        {
            "plan_id": {"type": "string", "description": "Existing Ops operation plan id."},
        },
        ["plan_id"],
    ),
    _tool(
        "approve_ops_operation_plan",
        "记录用户对 Ops 操作计划或单个特权步骤的确认。执行器会校验本轮用户原文必须精确为 confirm <plan_id> 或 confirm-step <plan_id> <step_id>，模型不能自行授权。",
        {
            "plan_id": {"type": "string", "description": "要确认的计划 ID。"},
            "scope": {
                "type": "string",
                "enum": ["plan", "step"],
                "description": "确认整个计划或单个步骤。",
            },
            "step_id": {
                "type": "string",
                "description": "scope=step 时必填的步骤 ID。",
            },
        },
        ["plan_id", "scope"],
    ),
    _tool(
        "execute_ops_operation_step",
        "执行已确认 Ops 计划中的一个受控 recipe 步骤。需要先完成 confirm <plan_id>；特权步骤还必须 confirm-step <plan_id> <step_id>。只会运行该步骤绑定 recipe_id 的白名单 runner；未知、未绑定或未接入 recipe 的步骤会 blocked，不会执行任意 shell。",
        {
            "plan_id": {"type": "string", "description": "已创建并确认的计划 ID。"},
            "step_id": {"type": "string", "description": "要执行的步骤 ID。"},
        },
        ["plan_id", "step_id"],
    ),
    _tool(
        "execute_ops_next_step",
        "按 OperationPlan 的 execution_order 执行当前下一步。模型不需要也不应该猜 step_id；如果下一步是特权步骤，仍必须先完成 confirm-step <plan_id> <step_id>。",
        {
            "plan_id": {"type": "string", "description": "已创建并确认的计划 ID。"},
        },
        ["plan_id"],
    ),
    _tool(
        "resolve_ops_blocked_step",
        "Reset a blocked Ops OperationPlan step to pending after runtime reinspection evidence has been collected. This does not authorize execution; privileged steps still require confirm-step again.",
        {
            "plan_id": {"type": "string", "description": "Existing Ops operation plan id."},
            "step_id": {"type": "string", "description": "Blocked step id to resolve."},
            "resolution_evidence": {
                "type": "string",
                "description": "Evidence from the latest runtime reinspection explaining why the blocked step can be retried.",
            },
        },
        ["plan_id", "step_id", "resolution_evidence"],
    ),
    _tool(
        "list_files",
        "列出当前 workspace 内的文件或目录。",
        {"path": {"type": "string", "description": "workspace 内相对路径，默认 ."}},
        [],
    ),
    _tool(
        "search_code",
        "在 Klonet 规范源码树中按字面量 grep 搜索源码。代码、接口、配置、启动脚本和报错类问题应优先用它定位真实源码证据。",
        {
            "query": {"type": "string", "description": "要搜索的函数名、路由、报错文本、配置项或关键词"},
            "path": {"type": "string", "description": "源码树内相对目录，默认搜索整个源码树"},
            "file_glob": {"type": "string", "description": "可选文件通配符，例如 *.py 或 mains/*.py"},
            "max_results": {"type": "integer", "description": "最多返回命中条数，默认 50"},
            "case_sensitive": {"type": "boolean", "description": "是否区分大小写，默认 false"},
        },
        ["query"],
    ),
    _tool(
        "read_source_file",
        "读取 Klonet 规范源码树中的真实源码文件。只能读取 klonet_knowledge/02_vemu_uestc_code 内文件，可指定行范围。",
        {
            "path": {"type": "string", "description": "源码树内相对文件路径，例如 mains/web_terminal_main.py"},
            "start_line": {"type": "integer", "description": "可选起始行号"},
            "end_line": {"type": "integer", "description": "可选结束行号"},
            "max_chars": {"type": "integer", "description": "最大返回字符数，默认 12000"},
        },
        ["path"],
    ),
    _tool(
        "list_source_files",
        "列出 Klonet 规范源码树中的源码文件相对路径，用于不知道入口文件时先缩小范围。",
        {
            "path": {"type": "string", "description": "源码树内相对目录，默认根目录"},
            "pattern": {"type": "string", "description": "可选相对路径通配符，例如 mains/*.py"},
            "max_results": {"type": "integer", "description": "最多返回文件数，默认 200"},
        },
        [],
    ),
    _tool(
        "read_file",
        "读取当前 workspace 内的文本文件。",
        {
            "path": {"type": "string", "description": "workspace 内相对路径"},
            "max_chars": {"type": "integer", "description": "最大返回字符数，默认 12000"},
        },
        ["path"],
    ),
    _tool(
        "write_file",
        "写入当前 workspace 内的文本文件。只能用于 Coding 模式。",
        {
            "path": {"type": "string", "description": "workspace 内相对路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
        ["path", "content"],
    ),
    _tool(
        "run_tests",
        "在当前 workspace 内运行安全白名单测试命令，例如 pytest -q。",
        {"command": {"type": "string", "description": "测试命令，默认 pytest -q"}},
        [],
    ),
    _tool(
        "show_diff",
        "查看当前 workspace 的 git diff。",
        {},
        [],
    ),
    _tool(
        "create_project_journal",
        "创建当前 user_id/project_id 对应的项目 Markdown 日志。",
        {"goal": {"type": "string", "description": "项目目标，可选"}},
        [],
    ),
    _tool(
        "read_project_journal",
        "读取当前 user_id/project_id 对应的项目 Markdown 日志。默认返回摘要，避免上下文过长。",
        {"max_chars": {"type": "integer", "description": "摘要最大字符数，默认 3000；传 0 表示读取全文"}},
        [],
    ),
    _tool(
        "append_journal_event",
        "向项目日志的指定章节追加事件。",
        {
            "section": {"type": "string", "description": "章节名，如 执行记录/遇到的问题/下一步"},
            "content": {"type": "string", "description": "要追加的内容"},
        },
        ["section", "content"],
    ),
    _tool(
        "update_project_status",
        "更新项目日志中的当前状态。",
        {"status": {"type": "string", "description": "新的项目状态"}},
        ["status"],
    ),
    _tool(
        "record_test_result",
        "记录测试与验证结果。",
        {"content": {"type": "string", "description": "测试命令、结果和结论"}},
        ["content"],
    ),
    _tool(
        "record_acceptance_gap",
        "记录功能差异与验收建议。",
        {"content": {"type": "string", "description": "预期功能、实际表现、差异和建议"}},
        ["content"],
    ),
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "加载指定技能的详细知识内容，在回答相关问题前调用",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "技能名称，必须是系统提示词中列出的可用技能之一",
                    }
                },
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_episode",
            "description": "【主动调用：记录今日事件】向当天的情景记忆（日记）追加文本。当你与用户完成了某个具体任务、探讨了某个技术难点（如跑完了一次实验、解决了一个 Bug）、或者用户做出了重要决定时，必须主动调用此工具记录。不要记录无意义的闲聊。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要追加的 Markdown 格式文本。请用 '## HH:MM 事件标题' 开头，简明扼要地记录事件的起因、过程和结论。",
                    }
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_memory",
            "description": "【危险操作：整篇覆盖】更新长期记忆文件 MEMORY.md。这会完全抹除旧文件。当项目状态有重大变更、或得出了需要长期记住的客观结论时调用。\n注意：你必须从你的系统提示词（System Prompt）中读取当前的 MEMORY.md 内容，在脑海中将新事实与旧内容进行融合，然后传入一份排版清晰、结构完整的全新 Markdown 文本！绝对不能只传入新增的碎片化句子，否则会导致历史数据永久丢失！",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "融合了新老知识的、完整的全新 MEMORY.md 文本内容。",
                    }
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_user",
            "description": "【危险操作：整篇覆盖】更新用户偏好文件 USER.md。这会完全抹除旧文件。当你发现用户的习惯、喜好或个人背景（如开发环境、工作流）发生变化时调用。\n注意：你必须从你的系统提示词（System Prompt）中读取当前的 USER.md 内容，将新发现的喜好与旧画像融合，传入一份完整的全新 Markdown 文本！千万不要只传一条新喜好，否则用户的旧档案会被清空！",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "融合了新老画像的、完整的全新 USER.md 文本内容。",
                    }
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "获取指定 URL 的网页内容，支持文本提取模式",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要访问的完整 URL"},
                    "extract_mode": {
                        "type": "string",
                        "description": "提取模式：text（纯文本，默认）或 raw（原始 HTML）",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最大返回字符数，默认 8000",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_todos",
            "description": (
                "创建或更新当前任务的 todolist。"
                "传入完整的 todos 数组（每次都是全量覆盖，而非增量）。"
                "用于：拆解多步骤任务、推进任务状态（pending → in_progress → completed）。"
                "约束：同一时间至多一个任务为 in_progress；等待用户输入时使用 waiting_user，当前模式无法执行时使用 blocked。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "完整的 todo 列表，按执行顺序排列",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer", "description": "序号，从 1 开始"},
                                "content": {"type": "string", "description": "这一步要做什么"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "waiting_user", "blocked"],
                                    "description": "状态",
                                },
                            },
                            "required": ["id", "content", "status"],
                        },
                    }
                },
                "required": ["todos"],
            },
        },
    },
]
