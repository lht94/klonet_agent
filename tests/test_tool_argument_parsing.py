from types import SimpleNamespace


def _tool_call(arguments):
    return SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="create_ops_operation_plan", arguments=arguments),
    )


def test_malformed_tool_arguments_return_observation_instead_of_raising():
    from klonet_agent.orchestrator import AgentOrchestrator

    orchestrator = object.__new__(AgentOrchestrator)
    args, error = orchestrator._parse_tool_arguments(
        _tool_call('{"operation":"deploy_platform","steps":[{bad json]}')
    )

    assert args == {}
    assert "invalid_tool_arguments_json" in error
    assert "The tool was not executed" in error
    assert "{bad json}" not in error


def test_tool_arguments_must_be_json_object():
    from klonet_agent.orchestrator import AgentOrchestrator

    orchestrator = object.__new__(AgentOrchestrator)
    args, error = orchestrator._parse_tool_arguments(_tool_call('["not", "an", "object"]'))

    assert args == {}
    assert "invalid_tool_arguments_type" in error


def test_valid_tool_arguments_are_returned():
    from klonet_agent.orchestrator import AgentOrchestrator

    orchestrator = object.__new__(AgentOrchestrator)
    args, error = orchestrator._parse_tool_arguments(
        _tool_call('{"operation":"deploy_platform","target":"lht"}')
    )

    assert error == ""
    assert args == {"operation": "deploy_platform", "target": "lht"}
