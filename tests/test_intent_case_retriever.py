"""Intent-case retrieval for semantic intent analysis."""

import json


def test_intent_case_loader_reads_structured_jsonl(tmp_path):
    from klonet_agent.knowledge.intent_cases import load_intent_cases

    case_file = tmp_path / "cases.jsonl"
    case_file.write_text(
        json.dumps(
            {
                "case_id": "platform_start_after_b_option",
                "tag": "intent_parse",
                "history_pattern": "A=environment setup, B=platform startup",
                "latest_query": "B",
                "handling_rule": "Resolve B as platform startup.",
                "semantic_frame": {"deployment_phase": "platform_startup"},
                "intent": {"operation": "platform_start"},
                "slots": {"runtime_path_policy": "verify_on_current_machine"},
                "safety": {"allow_direct_answer": False},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    cases = load_intent_cases(root=tmp_path)

    assert len(cases) == 1
    assert cases[0].case_id == "platform_start_after_b_option"
    assert cases[0].tag == "intent_parse"
    assert cases[0].semantic_frame["deployment_phase"] == "platform_startup"
    assert cases[0].safety["allow_direct_answer"] is False


def test_build_intent_case_query_keeps_recent_options_and_latest_query():
    from klonet_agent.knowledge.intent_cases import build_intent_case_query

    query = build_intent_case_query(
        "B",
        [
            {
                "role": "assistant",
                "content": (
                    "A：服务器是全新的，需要首次环境部署。\n"
                    "B：服务器上已经有 Klonet，只是没启动，需要平台启动。"
                ),
            }
        ],
    )

    assert "latest_query: B" in query
    assert "A：服务器是全新的" in query
    assert "B：服务器上已经有 Klonet" in query


def test_intent_case_retriever_matches_multiturn_b_option(tmp_path):
    from klonet_agent.knowledge.intent_cases import IntentCaseRetriever

    case_file = tmp_path / "deployment_cases.jsonl"
    case_file.write_text(
        json.dumps(
            {
                "case_id": "platform_start_after_b_option",
                "tag": "intent_parse",
                "history_pattern": "A：服务器是全新的，需要首次环境部署。B：服务器上已经有 Klonet，只是没启动。",
                "latest_query": "B",
                "normalized_query": "用户选择 B 路线",
                "handling_rule": "继承上文 B 选项，识别为已有平台启动。",
                "semantic_frame": {
                    "deployment_phase": "platform_startup",
                    "machine_role": "target_server",
                    "action_goal": "start_services",
                },
                "intent": {
                    "task_type": "deployment_guidance",
                    "operation": "platform_start",
                },
                "slots": {"excluded_meanings": ["environment_setup"]},
                "safety": {"allow_direct_answer": False},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    retriever = IntentCaseRetriever(root=tmp_path)

    matches = retriever.search(
        "recent_assistant: A：服务器是全新的，需要首次环境部署。B：服务器上已经有 Klonet，只是没启动。\nlatest_query: B",
        top_k=1,
        min_score=0.1,
    )

    assert len(matches) == 1
    assert matches[0].case_id == "platform_start_after_b_option"
    assert matches[0].intent["operation"] == "platform_start"
    assert matches[0].score > 0


def test_intent_case_retriever_uses_embedding_similarity_when_keywords_differ(tmp_path):
    from klonet_agent.knowledge.intent_cases import IntentCaseRetriever

    case_file = tmp_path / "cases.jsonl"
    case_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "case_id": "operator_workstation_tools",
                        "tag": "intent_parse",
                        "latest_query": "operator workstation preparation utilities",
                        "normalized_query": "local client tooling before remote operation",
                        "semantic_frame": {
                            "machine_role": "operator_local_pc",
                            "action_goal": "prepare_tools",
                        },
                        "intent": {"operation": "unknown"},
                        "safety": {"allow_direct_answer": False},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "case_id": "server_dependency_install",
                        "tag": "intent_parse",
                        "latest_query": "server middleware installation",
                        "normalized_query": "target host dependency setup",
                        "semantic_frame": {
                            "machine_role": "target_server",
                            "action_goal": "install_dependencies",
                        },
                        "intent": {"operation": "environment_setup"},
                        "safety": {"allow_direct_answer": False},
                    },
                    ensure_ascii=False,
                ),
            ]
        ),
        encoding="utf-8",
    )

    def embed(text: str) -> list[float]:
        lowered = text.lower()
        if "laptop" in lowered or "workstation" in lowered or "client tooling" in lowered:
            return [1.0, 0.0]
        if "server" in lowered or "middleware" in lowered:
            return [0.0, 1.0]
        return [0.0, 0.0]

    retriever = IntentCaseRetriever(root=tmp_path, embedding_provider=embed)

    matches = retriever.search(
        "what should I install on my laptop before operating it remotely",
        top_k=1,
        min_score=0.2,
    )

    assert len(matches) == 1
    assert matches[0].case_id == "operator_workstation_tools"
    assert matches[0].retrieval_mode == "hybrid"
    assert matches[0].semantic_score > matches[0].keyword_score


def test_intent_case_prompt_cases_drop_conflicting_high_score_cases(tmp_path):
    from klonet_agent.knowledge.intent_cases import IntentCaseRetriever

    case_file = tmp_path / "cases.jsonl"
    case_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "case_id": "deploy_means_startup",
                        "tag": "intent_parse",
                        "latest_query": "deploy platform",
                        "semantic_frame": {"deployment_phase": "platform_startup"},
                        "intent": {"operation": "platform_start"},
                        "safety": {"allow_direct_answer": False},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "case_id": "deploy_means_environment_setup",
                        "tag": "intent_parse",
                        "latest_query": "deploy platform",
                        "semantic_frame": {"deployment_phase": "environment_setup"},
                        "intent": {"operation": "environment_setup"},
                        "safety": {"allow_direct_answer": False},
                    },
                    ensure_ascii=False,
                ),
            ]
        ),
        encoding="utf-8",
    )

    retriever = IntentCaseRetriever(root=tmp_path)

    prompt_cases = retriever.search_for_prompt(
        "latest_query: deploy platform",
        top_k=2,
        min_score=0.1,
    )

    assert prompt_cases == ()


def test_default_intent_cases_cover_key_klonet_failures():
    from klonet_agent.knowledge.intent_cases import (
        IntentCaseRetriever,
        build_intent_case_query,
    )

    retriever = IntentCaseRetriever()
    b_query = build_intent_case_query(
        "B",
        [
            {
                "role": "assistant",
                "content": (
                    "A：服务器是全新的，需要首次环境部署。\n"
                    "B：服务器上已经有 Klonet，只是没启动，需要平台启动。"
                ),
            }
        ],
    )
    computer_query = build_intent_case_query(
        "部署平台之前，电脑里需要下载什么软件吗？",
        [],
    )
    late_supplement_query = build_intent_case_query(
        "klonet平台",
        [
            {
                "role": "user",
                "content": "我在使用平台之前，电脑里需要下载什么软件吗？",
            },
            {
                "role": "assistant",
                "content": "场景一：你在浏览器里用平台（普通用户）。场景二：你要部署运行平台。",
            },
        ],
    )

    b_matches = retriever.search(b_query, top_k=2, min_score=0.1)
    computer_matches = retriever.search(computer_query, top_k=2, min_score=0.1)
    late_supplement_matches = retriever.search(
        late_supplement_query,
        top_k=3,
        min_score=0.1,
    )

    assert any(match.case_id == "platform_start_after_b_option" for match in b_matches)
    assert any(
        match.case_id == "operator_pc_tool_preparation" for match in computer_matches
    )
    assert any(
        match.case_id == "late_platform_name_supplement_usage"
        for match in late_supplement_matches
    )
