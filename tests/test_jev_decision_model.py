"""Jev decision transport, rollout and schema contract tests."""

from __future__ import annotations

import json

import pytest


def _response(answers, *, confidence=None, usage=None):
    payload = {"answers": answers}
    if confidence is not None:
        payload["confidence"] = confidence
    if usage is not None:
        payload["usage"] = usage
    return payload


def test_stable_gray_bucket_uses_user_and_project_identity():
    from klonet_agent.llm.decision import stable_gray_bucket

    first = stable_gray_bucket("alice", "project-a")
    second = stable_gray_bucket("alice", "project-a")

    assert first == second
    assert 0 <= first < 100
    assert stable_gray_bucket("alice", "project-b") != first


def test_typesafe_client_builds_typed_request_and_parses_metadata():
    from klonet_agent.llm.decision import TypeSafeJevClient

    captured = {}

    def transport(url, headers, payload, timeout):
        captured.update(
            url=url, headers=headers, payload=payload, timeout=timeout,
        )
        return _response(
            {
                "scope": {
                    "value": "klonet",
                    "probabilities": {"klonet": 0.97, "general": 0.03},
                },
                "requires_retrieval": {
                    "value": True,
                    "probability": 0.96,
                },
            },
            confidence={"scope": 0.93},
            usage={"input_tokens": 123},
        )

    client = TypeSafeJevClient(
        api_key="secret-key",
        base_url="https://typesafe.example/v1/systemone",
        model="jev-test",
        timeout=2.5,
        transport=transport,
    )
    result = client.evaluate(
        state={"request": "启动 Klonet"},
        questions={
            "scope": {"type": "choice", "options": ["klonet", "general"]},
            "requires_retrieval": {"type": "boolean"},
        },
    )

    assert captured["payload"]["model"] == "jev-test"
    assert captured["payload"]["state"] == {"request": "启动 Klonet"}
    assert captured["timeout"] == 2.5
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert "secret-key" not in repr(client)
    assert result.answers == {
        "scope": "klonet",
        "requires_retrieval": True,
    }
    assert result.probabilities["scope"]["klonet"] == 0.97
    assert result.confidences["scope"] == 0.93
    assert result.input_tokens == 123
    assert result.provider == "typesafe"
    assert result.model == "jev-test"
    assert result.latency_seconds >= 0


def test_typesafe_client_redacts_sensitive_state_before_transport():
    from klonet_agent.llm.decision import TypeSafeJevClient

    captured = {}

    def transport(_url, _headers, payload, _timeout):
        captured["state"] = payload["state"]
        return _response({"scope": {"value": "klonet", "probability": 1.0}})

    client = TypeSafeJevClient(
        api_key="transport-secret",
        transport=transport,
    )
    client.evaluate(
        state={
            "request": "token=abc123456 password=hunter2",
            "nested": {"api_key": "sk-sensitive-value"},
        },
        questions={"scope": {"type": "choice", "options": ["klonet"]}},
    )

    encoded = json.dumps(captured["state"], ensure_ascii=False)
    assert "abc123456" not in encoded
    assert "hunter2" not in encoded
    assert "sk-sensitive-value" not in encoded


def test_typesafe_client_rejects_unknown_choice_and_missing_answers():
    from klonet_agent.llm.decision import DecisionModelError, TypeSafeJevClient

    client = TypeSafeJevClient(
        api_key="secret",
        transport=lambda *_args: _response(
            {"scope": {"value": "invented", "probability": 0.99}}
        ),
    )
    with pytest.raises(DecisionModelError, match="scope"):
        client.evaluate(
            state={"request": "hello"},
            questions={
                "scope": {"type": "choice", "options": ["klonet", "general"]},
            },
        )

    missing = TypeSafeJevClient(
        api_key="secret", transport=lambda *_args: {"answers": {}},
    )
    with pytest.raises(DecisionModelError, match="scope"):
        missing.evaluate(
            state={"request": "hello"},
            questions={
                "scope": {"type": "choice", "options": ["klonet", "general"]},
            },
        )


@pytest.mark.parametrize(
    ("noul", "expected", "confidence"),
    [(0.04, False, 0.96), (0.96, True, 0.96), (0.5, True, 0.5)],
)
def test_typesafe_client_parses_official_noul_probability(
    noul, expected, confidence,
):
    from klonet_agent.llm.decision import TypeSafeJevClient

    client = TypeSafeJevClient(
        api_key="secret",
        transport=lambda *_args: {
            "model": "jev-1.13.0",
            "answers": {"flag": {"type": "noul", "noul": noul}},
            "usage": {"input_tokens": 10},
        },
    )
    result = client.evaluate(
        state="hello",
        questions={"flag": {"type": "boolean"}},
    )

    assert result.answers["flag"] is expected
    assert result.confidences["flag"] == pytest.approx(confidence)
    assert result.probabilities["flag"] == pytest.approx(
        {"true": noul, "false": 1.0 - noul}
    )
    assert result.version == "jev-1.13.0"


def test_jev_decision_model_declares_bounded_turn_and_privileged_questions():
    from klonet_agent.llm.decision import JevDecisionModel

    calls = []

    class Evaluator:
        def evaluate(self, *, state, questions):
            calls.append((state, questions))
            answers = {}
            confidences = {}
            for name, question in questions.items():
                if question["type"] == "boolean":
                    answers[name] = False
                else:
                    answers[name] = question["options"][0]
                confidences[name] = 0.99
            from klonet_agent.llm.decision import DecisionResult
            return DecisionResult(answers=answers, confidences=confidences)

    model = JevDecisionModel(Evaluator())
    turn = model.classify_turn({"request": "什么是 Klonet"})
    privileged = model.classify_privileged_intent({"request": "检查状态"})

    assert turn.answers["scope"] == "klonet"
    assert set(calls[0][1]) == {
        "scope", "task_type", "operation", "deployment_phase",
        "action_goal", "requires_retrieval",
        "requires_environment_diagnosis", "clarification_required",
        "is_correction",
    }
    assert privileged.answers["intent"] == "conversation"
    assert set(calls[1][1]) == {
        "intent", "goal_clarity", "goal_relation", "goal_kind",
        "operation", "scope", "requires_execution",
    }


def test_intent_analyzer_skips_llm_for_confident_non_retrieval_decision():
    from klonet_agent.knowledge.intent_analyzer import IntentAnalyzer
    from klonet_agent.llm.decision import DecisionResult

    class NeverLLM:
        def complete(self, **_kwargs):
            raise AssertionError("generative LLM should be skipped")

    class Decisions:
        def classify_turn(self, _state):
            answers = {
                "scope": "general",
                "task_type": "general",
                "operation": "unknown",
                "deployment_phase": "unknown",
                "action_goal": "explain_concept",
                "requires_retrieval": False,
                "requires_environment_diagnosis": False,
                "clarification_required": False,
                "is_correction": False,
            }
            return DecisionResult(
                answers=answers,
                confidences={key: 0.96 for key in answers},
                latency_seconds=0.04,
            )

    analysis = IntentAnalyzer(
        NeverLLM(), decision_model=Decisions(), min_decision_confidence=0.85,
    ).analyze("Python 的 GIL 是什么？")

    assert analysis.intent.scope == "general"
    assert analysis.intent.requires_retrieval is False
    assert analysis.used_model is False
    assert analysis.used_decision_model is True
    assert analysis.decision_latency_seconds == 0.04


def test_intent_analyzer_falls_back_to_llm_on_low_jev_confidence():
    from types import SimpleNamespace

    from klonet_agent.knowledge.intent_analyzer import IntentAnalyzer
    from klonet_agent.llm.decision import DecisionResult

    class LLM:
        calls = 0

        def complete(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=(
                    '{"scope":"klonet","task_type":"concept",'
                    '"operation":"unknown","requires_retrieval":true,'
                    '"confidence":0.91}'
                )))],
                usage=SimpleNamespace(total_tokens=7),
            )

    class Decisions:
        def classify_turn(self, _state):
            return DecisionResult(
                answers={"scope": "general"},
                confidences={"scope": 0.4},
            )

    llm = LLM()
    analysis = IntentAnalyzer(
        llm, decision_model=Decisions(), min_decision_confidence=0.85,
    ).analyze("Klonet 是什么？")

    assert llm.calls == 1
    assert analysis.intent.scope == "klonet"
    assert analysis.used_decision_model is False
    assert analysis.decision_fallback_reason == "low_confidence"


def test_intent_analyzer_keeps_jev_enums_when_llm_builds_retrieval_plan():
    from types import SimpleNamespace

    from klonet_agent.knowledge.intent_analyzer import IntentAnalyzer
    from klonet_agent.llm.decision import DecisionResult

    class LLM:
        def complete(self, **kwargs):
            prompt = kwargs["messages"][-1]["content"]
            assert "Jev 已确定字段" in prompt
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=(
                    '{"scope":"general","task_type":"concept",'
                    '"operation":"unknown","target":"web_terminal",'
                    '"symptom":"port_conflict","requires_retrieval":false,'
                    '"standalone_query":"Klonet web terminal 端口冲突",'
                    '"retrieval_tasks":[{"store":"source_code",'
                    '"purpose":"定位端口实现","keyword_queries":["web_terminal"],'
                    '"semantic_queries":[],"exact_terms":["web_terminal"]}]}'
                )))],
                usage=SimpleNamespace(total_tokens=11),
            )

    answers = {
        "scope": "klonet",
        "task_type": "troubleshooting",
        "operation": "platform_start",
        "deployment_phase": "troubleshooting",
        "action_goal": "inspect_error",
        "requires_retrieval": True,
        "requires_environment_diagnosis": True,
        "clarification_required": False,
        "is_correction": False,
    }

    class Decisions:
        def classify_turn(self, _state):
            return DecisionResult(
                answers=answers,
                confidences={key: 0.94 for key in answers},
            )

    analysis = IntentAnalyzer(
        LLM(), decision_model=Decisions(),
    ).analyze("Klonet web_terminal 端口冲突")

    assert analysis.intent.scope == "klonet"
    assert analysis.intent.task_type == "concept"
    assert analysis.intent.operation == "unknown"
    assert analysis.intent.requires_retrieval is True
    assert analysis.intent.target == "web_terminal"
    assert analysis.retrieval_plan is not None
    assert analysis.used_model is True
    assert analysis.used_decision_model is True


def test_privileged_classifier_uses_confident_jev_without_command_authority():
    from klonet_agent.llm.decision import DecisionResult
    from klonet_agent.ops.privileged.intent import PrivilegedIntentClassifier

    class NeverLLM:
        def complete(self, **_kwargs):
            raise AssertionError("legacy classifier should be skipped")

    answers = {
        "intent": "mutating_action",
        "goal_clarity": "discoverable",
        "goal_relation": "new",
        "goal_kind": "execution",
        "operation": "restart",
        "scope": "component",
        "requires_execution": True,
    }

    class Decisions:
        def classify_privileged_intent(self, _state):
            return DecisionResult(
                answers=answers,
                confidences={key: 0.97 for key in answers},
            )

    decision = PrivilegedIntentClassifier(
        NeverLLM(), decision_model=Decisions(),
    ).classify("重启 worker，不要动 master")

    assert decision.intent == "mutating_action"
    assert decision.operation == "restart"
    assert decision.components == ("worker",)
    assert decision.command == ""
    assert decision.requires_execution is True


def test_privileged_classifier_falls_back_when_jev_fails():
    from types import SimpleNamespace

    from klonet_agent.ops.privileged.intent import PrivilegedIntentClassifier

    class LLM:
        calls = 0

        def complete(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=(
                    '{"intent":"conversation","goal_clarity":"clear",'
                    '"goal_relation":"new","goal_kind":"conversation",'
                    '"operation":"none","scope":"none","components":[],'
                    '"requires_execution":false,"command":"",'
                    '"confidence":0.9,"reason":"discussion",'
                    '"clarification_question":"","plan_reference":""}'
                )
            ))])

    class Decisions:
        def classify_privileged_intent(self, _state):
            raise TimeoutError("provider timeout")

    llm = LLM()
    decision = PrivilegedIntentClassifier(
        llm, decision_model=Decisions(),
    ).classify("解释一下重启流程")

    assert decision.intent == "conversation"
    assert llm.calls == 1


def test_privileged_classifier_ignores_untrusted_jev_goal_fields():
    from klonet_agent.llm.decision import DecisionResult
    from klonet_agent.ops.privileged.intent import PrivilegedIntentClassifier

    class NeverLLM:
        def complete(self, **_kwargs):
            raise AssertionError("legacy classifier should be skipped")

    class Decisions:
        def classify_privileged_intent(self, _state):
            return DecisionResult(
                answers={
                    "intent": "conversation",
                    "requires_execution": False,
                    "goal_kind": "causal_diagnosis",
                    "goal_clarity": "discoverable",
                    "operation": "restart",
                    "scope": "platform",
                },
                confidences={
                    "intent": 0.99,
                    "requires_execution": 0.99,
                    "goal_kind": 0.99,
                    "goal_clarity": 0.99,
                    "operation": 0.99,
                    "scope": 0.99,
                },
            )

    decision = PrivilegedIntentClassifier(
        NeverLLM(), decision_model=Decisions(),
    ).classify("解释一下刚才为什么会重启")

    assert decision.intent == "conversation"
    assert decision.goal_kind == "conversation"
    assert decision.operation == "none"
    assert decision.scope == "none"


def test_configured_jev_model_respects_enabled_key_and_gray_bucket(monkeypatch):
    from klonet_agent.llm import decision as module

    monkeypatch.setattr(module, "JEV_ENABLED", True)
    monkeypatch.setattr(module, "JEV_API_KEY_ENV", "TEST_TYPESAFE_KEY")
    monkeypatch.setattr(module, "JEV_GRAY_PERCENT", 100)
    monkeypatch.setenv("TEST_TYPESAFE_KEY", "configured-secret")

    model = module.configured_jev_decision_model("alice", "project-a")
    assert isinstance(model, module.JevDecisionModel)
    assert "configured-secret" not in repr(model.evaluator)

    monkeypatch.setattr(module, "JEV_GRAY_PERCENT", 0)
    assert module.configured_jev_decision_model("alice", "project-a") is None

    monkeypatch.setattr(module, "JEV_GRAY_PERCENT", 100)
    monkeypatch.delenv("TEST_TYPESAFE_KEY")
    assert module.configured_jev_decision_model("alice", "project-a") is None


def test_orchestrator_shares_injected_decision_model_with_both_routes(tmp_path):
    from klonet_agent.agents import get_profile
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession

    class FakeLLM:
        pass

    class Decisions:
        pass

    decisions = Decisions()
    orchestrator = AgentOrchestrator(
        profile=get_profile("ops"),
        session=AgentSession(
            user_id="jev-eval", project_id="routing", mode="ops",
            workspace_path=tmp_path / "workspace",
            journal_path=tmp_path / "journal.md",
        ),
        llm=FakeLLM(),
        memory_store=MemoryStore.for_session(
            tmp_path / "memory", "jev-eval", "routing",
        ),
        decision_model=decisions,
    )

    assert orchestrator.intent_analyzer.decision_model is decisions
    assert orchestrator.privileged_supervisor.classifier.decision_model is decisions
