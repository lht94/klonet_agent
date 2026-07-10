"""项目日志测试。"""

import sys
from pathlib import Path
from types import SimpleNamespace

from tests.helpers import local_temp_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_project_journal():
    from klonet_agent.journal.project_journal import ProjectJournal

    with local_temp_dir() as temp_dir:
        journal = ProjectJournal(temp_dir / "u1" / "p1.md", "u1", "p1")
        journal.ensure("实现 Klonet 测试功能")
        journal.update_status("开发中")
        journal.append_event("执行记录", "完成需求分析")
        journal.record_test_result("pytest -q 通过")

        text = journal.read()

    assert "实现 Klonet 测试功能" in text
    assert "当前状态：开发中" in text
    assert "完成需求分析" in text
    assert "pytest -q 通过" in text


def test_project_journal_summary_is_shorter_than_full_log():
    """项目日志应该能生成摘要，避免每次全量注入上下文。"""

    from klonet_agent.journal.project_journal import ProjectJournal

    with local_temp_dir() as temp_dir:
        journal = ProjectJournal(temp_dir / "u1" / "p1.md", "u1", "p1")
        journal.ensure("实现 Klonet 测试功能")
        journal.update_status("开发中")
        for index in range(20):
            journal.append_event("执行记录", f"完成第 {index} 个开发步骤")

        full_text = journal.read()
        summary = journal.summary(max_chars=300)

    assert "项目日志摘要" in summary
    assert "当前状态：开发中" in summary
    assert "实现 Klonet 测试功能" in summary
    assert len(summary) <= 300
    assert len(summary) < len(full_text)


def test_read_project_journal_tool_can_return_summary():
    """读取项目日志工具应该支持摘要返回。"""

    from klonet_agent.journal.project_journal import ProjectJournal
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.executor import ToolExecutor

    with local_temp_dir() as temp_dir:
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        journal = ProjectJournal.from_session(session)
        journal.ensure("实现 Klonet 测试功能")
        for index in range(20):
            journal.append_event("执行记录", f"完成第 {index} 个开发步骤")

        executor = ToolExecutor(
            session=session,
            allowed_tools={"read_project_journal"},
        )
        result = executor.run("read_project_journal", {"max_chars": 260})

    assert "项目日志摘要" in result
    assert len(result) <= 260


def test_project_journal_existing_placeholder_goal_can_be_filled():
    """已存在的空模板日志应该允许后续补写项目目标。"""

    from klonet_agent.journal.project_journal import ProjectJournal

    with local_temp_dir() as temp_dir:
        journal = ProjectJournal(temp_dir / "u1" / "p1.md", "u1", "p1")
        journal.ensure()
        journal.ensure("研究 Klonet 从单 Worker 升级到多 Worker")

        text = journal.read()

    assert "研究 Klonet 从单 Worker 升级到多 Worker" in text
    assert "## 项目目标\n待补充。" not in text


def test_project_journal_goal_can_be_extended_without_overwriting():
    """项目目标应该支持后续补充，而不是只能写一个。"""

    from klonet_agent.journal.project_journal import ProjectJournal

    with local_temp_dir() as temp_dir:
        journal = ProjectJournal(temp_dir / "u1" / "p1.md", "u1", "p1")
        journal.ensure("研究 Klonet 多 Worker 架构")
        journal.merge_goal("补充断电恢复和数据持久化机制分析")
        journal.merge_goal("补充断电恢复和数据持久化机制分析")

        text = journal.read()

    assert "研究 Klonet 多 Worker 架构" in text
    assert "补充断电恢复和数据持久化机制分析" in text
    assert text.count("补充断电恢复和数据持久化机制分析") == 1


def test_project_journal_goal_merge_skips_similar_rephrasing():
    """LLM 改写出的相似目标不应该频繁重复追加。"""

    from klonet_agent.journal.project_journal import ProjectJournal

    with local_temp_dir() as temp_dir:
        journal = ProjectJournal(temp_dir / "u1" / "p1.md", "u1", "p1")
        journal.ensure("研究 Klonet 多 Worker 架构与资源调度机制")
        journal.merge_goal("分析 Klonet 多 Worker 架构设计和资源调度机制")
        journal.merge_goal("补充断电恢复和数据持久化机制分析")

        text = journal.read()

    assert "研究 Klonet 多 Worker 架构与资源调度机制" in text
    assert "分析 Klonet 多 Worker 架构设计和资源调度机制" not in text
    assert "补充断电恢复和数据持久化机制分析" in text


def test_project_journal_goal_merge_uses_embedding_similarity_first():
    """有 embedding provider 时，应优先用向量相似度拦截语义重复目标。"""

    from klonet_agent.journal.project_journal import ProjectJournal

    vectors = {
        "研究容器数据持久化和断电恢复机制": (1.0, 0.0),
        "分析断电后容器数据如何保持和恢复": (0.99, 0.01),
        "研究多 Worker 资源调度算法": (0.0, 1.0),
    }

    def embed(text):
        return vectors[text]

    with local_temp_dir() as temp_dir:
        journal = ProjectJournal(temp_dir / "u1" / "p1.md", "u1", "p1")
        journal.ensure("研究容器数据持久化和断电恢复机制")
        journal.merge_goal("分析断电后容器数据如何保持和恢复", embedding_provider=embed)
        journal.merge_goal("研究多 Worker 资源调度算法", embedding_provider=embed)

        text = journal.read()

    assert "研究容器数据持久化和断电恢复机制" in text
    assert "分析断电后容器数据如何保持和恢复" not in text
    assert "研究多 Worker 资源调度算法" in text


def test_llm_project_journal_maintainer_writes_structured_update():
    """项目日志维护子 Agent 应使用 LLM 的结构化决策更新日志。"""

    from klonet_agent.journal.maintainer import ProjectJournalMaintainer
    from klonet_agent.journal.project_journal import ProjectJournal

    class FakeJournalLLM:
        def complete(self, messages, tools=None):
            assert tools is None
            assert "Project Journal Agent" in messages[0]["content"]
            assert "mentor" in messages[0]["content"]
            assert "coding" in messages[0]["content"]
            assert "ops" in messages[0]["content"]
            content = """
            {
              "should_update": true,
              "confidence": "high",
              "goal": "研究 Klonet 从单 Worker 架构升级到多 Worker 架构，重点分析资源调度、拓扑切分、跨 Worker 链路、状态同步和进度聚合机制。",
              "current_status": "正在梳理项目背景与研究意义",
              "events": ["确认项目主题与 Klonet 单 Worker 到多 Worker 升级相关。"],
              "next_step": "继续补充研究意义、技术路线和系统设计。",
              "evidence": ["用户明确提到单 Worker 升级到多 Worker"]
            }
            """
            message = SimpleNamespace(content=content)
            choice = SimpleNamespace(message=message)
            return SimpleNamespace(choices=[choice])

    with local_temp_dir() as temp_dir:
        journal = ProjectJournal(temp_dir / "u1" / "p1.md", "u1", "p1")
        maintainer = ProjectJournalMaintainer(journal, llm=FakeJournalLLM())
        decision = maintainer.maintain_turn(
            "我的项目是从单 Worker 升级到多 Worker，想写背景意义",
            "可以从资源扩展、分布式调度和跨 Worker 网络来写。",
        )

        text = journal.read()

    assert decision.should_update is True
    assert "单 Worker 架构升级到多 Worker 架构" in text
    assert "当前状态：正在梳理项目背景与研究意义" in text
    assert "继续补充研究意义、技术路线和系统设计" in text


def test_project_journal_maintainer_uses_llm_for_arbitrary_learning_topic():
    """任意实质学习主题都应由 LLM 子 Agent 判断并结构化写入。"""

    from klonet_agent.journal.maintainer import ProjectJournalMaintainer
    from klonet_agent.journal.project_journal import ProjectJournal

    class TopicLLM:
        def complete(self, messages, tools=None):
            assert "断电后如何重启" in messages[0]["content"]
            message = SimpleNamespace(
                content="""
                {
                  "should_update": true,
                  "confidence": "medium",
                  "goal": "",
                  "current_status": "正在学习 Klonet 断电重启与拓扑恢复机制",
                  "events": ["讨论 Klonet 断电后重启恢复流程，涉及基础服务启动、拓扑重新部署和容器数据持久化。"],
                  "next_step": "结合实际环境确认 MySQL、Redis、Docker、OVS 和 screen 服务的恢复步骤。",
                  "evidence": ["用户询问 Klonet 断电后如何重启以及拓扑、容器数据如何恢复"]
                }
                """
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    with local_temp_dir() as temp_dir:
        journal = ProjectJournal(temp_dir / "u1" / "p1.md", "u1", "p1")
        maintainer = ProjectJournalMaintainer(journal, llm=TopicLLM())
        decision = maintainer.maintain_turn(
            "我想要了解klonet断电后如何重启，包括拓扑，容器里的数据等，怎么恢复呢",
            "Klonet 断电后需要重启基础服务，并重新部署拓扑恢复容器和 OVS 运行态。",
        )

        text = journal.read()

    assert decision.should_update is True
    assert "正在学习 Klonet 断电重启与拓扑恢复机制" in text
    assert "讨论 Klonet 断电后重启恢复流程" in text


def test_project_journal_maintainer_can_extend_existing_goal():
    """日志 Agent 判断目标更新时，应追加新目标而不是忽略。"""

    from klonet_agent.journal.maintainer import ProjectJournalMaintainer
    from klonet_agent.journal.project_journal import ProjectJournal

    class GoalUpdateLLM:
        def complete(self, messages, tools=None):
            message = SimpleNamespace(
                content="""
                {
                  "should_update": true,
                  "confidence": "high",
                  "goal": "补充容器数据持久化和断电恢复机制研究",
                  "current_status": "",
                  "events": [],
                  "next_step": "",
                  "evidence": ["用户补充了新的项目目标"]
                }
                """
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    with local_temp_dir() as temp_dir:
        journal = ProjectJournal(temp_dir / "u1" / "p1.md", "u1", "p1")
        journal.ensure("研究 Klonet 多 Worker 架构")
        maintainer = ProjectJournalMaintainer(journal, llm=GoalUpdateLLM())
        maintainer.maintain_turn(
            "我还想补充容器数据持久化和断电恢复机制",
            "可以把它作为项目的新增目标记录。",
        )

        text = journal.read()

    assert "研究 Klonet 多 Worker 架构" in text
    assert "补充容器数据持久化和断电恢复机制研究" in text


def test_project_journal_maintainer_passes_embedding_provider_to_goal_merge():
    """日志 Agent 合并目标时应使用向量相似度避免重复目标。"""

    from klonet_agent.journal.maintainer import ProjectJournalMaintainer
    from klonet_agent.journal.project_journal import ProjectJournal

    class GoalUpdateLLM:
        def complete(self, messages, tools=None):
            message = SimpleNamespace(
                content="""
                {
                  "should_update": true,
                  "confidence": "high",
                  "goal": "分析断电后容器数据如何保持和恢复",
                  "current_status": "",
                  "events": [],
                  "next_step": "",
                  "evidence": ["用户补充了相似目标"]
                }
                """
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    vectors = {
        "研究容器数据持久化和断电恢复机制": (1.0, 0.0),
        "分析断电后容器数据如何保持和恢复": (0.99, 0.01),
    }

    def embed(text):
        return vectors[text]

    with local_temp_dir() as temp_dir:
        journal = ProjectJournal(temp_dir / "u1" / "p1.md", "u1", "p1")
        journal.ensure("研究容器数据持久化和断电恢复机制")
        maintainer = ProjectJournalMaintainer(
            journal,
            llm=GoalUpdateLLM(),
            embedding_provider=embed,
        )
        maintainer.maintain_turn(
            "我还想补充断电后容器数据如何保持和恢复",
            "可以把它作为项目目标。",
        )

        text = journal.read()

    assert "研究容器数据持久化和断电恢复机制" in text
    assert "分析断电后容器数据如何保持和恢复" not in text


def test_project_journal_maintainer_without_llm_does_not_hardcode_project_topics():
    """没有 LLM 子 Agent 时不应靠代码特例猜测具体项目。"""

    from klonet_agent.journal.maintainer import ProjectJournalMaintainer
    from klonet_agent.journal.project_journal import ProjectJournal

    with local_temp_dir() as temp_dir:
        journal = ProjectJournal(temp_dir / "u1" / "p1.md", "u1", "p1")
        maintainer = ProjectJournalMaintainer(journal, llm=None)
        decision = maintainer.maintain_turn(
            "我的项目是从单 Worker 升级到多 Worker，想写背景意义",
            "可以从资源扩展、分布式调度和跨 Worker 网络来写。",
        )

        text = journal.read()

    assert decision.should_update is False
    assert "单 Worker 架构升级到多 Worker 架构" not in text
    assert "## 项目目标\n待补充。" in text


def test_mentor_orchestrator_invokes_project_journal_maintainer():
    """Mentor 最终回答后应触发项目日志维护子 Agent。"""

    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession

    class FakeUsage:
        total_tokens = 10

    class FakeLLM:
        def complete(self, messages, tools=None, **kwargs):
            message = SimpleNamespace(content="已给出当前建议。", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=FakeUsage())

    class FakeMaintainer:
        def __init__(self):
            self.calls = []

        def maintain_turn(self, user_input, reply, mode="mentor"):
            self.calls.append((user_input, reply, mode))

    with local_temp_dir() as temp_dir:
        session = AgentSession(
            user_id="u1",
            project_id="p1",
            workspace_path=temp_dir / "workspace",
            journal_path=temp_dir / "journal.md",
        )
        maintainer = FakeMaintainer()
        orchestrator = AgentOrchestrator(
            profile=get_profile("mentor"),
            session=session,
            llm=FakeLLM(),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u1", "p1"),
            journal_maintainer=maintainer,
        )
        history = orchestrator.init_history()
        orchestrator.single_chat("我的项目是从单 Worker 升级到多 Worker", history, 0)

    assert maintainer.calls == [
        ("我的项目是从单 Worker 升级到多 Worker", "已给出当前建议。", "mentor")
    ]
