"""工具注册表。

这里保存给大模型看的工具 schema，例如 run_command、load_skill、search_knowledge 等。
它只负责声明工具能力，不负责真正执行工具。
"""


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
        "检索 Klonet 知识库。Mentor 回答 Klonet 问题前必须优先调用；Coding 写代码前用于查规范和相似实现。",
        {
            "query": {"type": "string", "description": "检索问题或关键词"},
            "top_k": {"type": "integer", "description": "返回条数，默认 5"},
        },
        ["query"],
    ),
    _tool(
        "list_files",
        "列出当前 workspace 内的文件或目录。",
        {"path": {"type": "string", "description": "workspace 内相对路径，默认 ."}},
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
        "读取当前 user_id/project_id 对应的项目 Markdown 日志。",
        {},
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
                "约束：同一时间至多一个任务为 in_progress。"
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
                                    "enum": ["pending", "in_progress", "completed"],
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
