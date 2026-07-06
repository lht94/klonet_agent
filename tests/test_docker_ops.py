def test_inspect_docker_containers_uses_fixed_sudo_helper_command():
    from klonet_agent.tools.docker_ops import inspect_docker_containers

    calls = []

    def runner(command):
        calls.append(command)
        return "containers=mysql-vemu Up 2 hours"

    result = inspect_docker_containers({"name": "mysql-vemu"}, command_runner=runner)

    assert calls == [[
        "sudo",
        "-n",
        "/usr/local/bin/klonet-agent-op",
        "inspect-docker-containers",
        "--execute",
        "--name",
        "mysql-vemu",
    ]]
    assert "mysql-vemu" in result


def test_inspect_docker_containers_rejects_unsafe_name():
    from klonet_agent.tools.docker_ops import inspect_docker_containers

    assert "invalid_container_name" in inspect_docker_containers({"name": "mysql;id"})
