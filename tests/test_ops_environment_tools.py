"""Read-only environment diagnostic tool tests."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_redacts_common_secret_shapes():
    from klonet_agent.tools.environment import redact_sensitive_text

    text = "PASSWORD=abc123\nAuthorization: Bearer token-value\napi_key = sk-test"

    redacted = redact_sensitive_text(text)

    assert "abc123" not in redacted
    assert "token-value" not in redacted
    assert "sk-test" not in redacted
    assert "[REDACTED]" in redacted


def test_read_only_probe_rejects_unregistered_command():
    from klonet_agent.tools.environment import run_read_only_probe

    result = run_read_only_probe("rm -rf /")

    assert result.status == "unchecked"
    assert "not allowlisted" in result.detail


def test_log_reader_refuses_env_files():
    from klonet_agent.tools.environment import read_klonet_logs
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        env_file = temp_dir / ".env"
        env_file.write_text("PASSWORD=abc123", encoding="utf-8")

        result = read_klonet_logs({"path": str(env_file)})

    assert result.startswith("Error:")
    assert "refused" in result.lower()


def test_log_reader_reports_resolved_path_mtime_and_size():
    from klonet_agent.tools.environment import read_klonet_logs
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        log_file = temp_dir / "error.log"
        log_file.write_text("first line\nlatest line\n", encoding="utf-8")

        result = read_klonet_logs({"path": str(log_file), "max_chars": 100})

    assert "resolved_path=" in result
    assert "mtime=" in result
    assert "size_bytes=" in result
    assert "latest line" in result


def test_environment_tools_are_registered_for_llm():
    from klonet_agent.tools.registry import TOOLS

    tool_names = {item["function"]["name"] for item in TOOLS}

    assert "render_klonet_config" in tool_names
    assert "inspect_ops_context" in tool_names
    assert "inspect_platform_instances" in tool_names
    assert "inspect_system_environment" in tool_names
    assert "inspect_klonet_runtime" in tool_names
    assert "inspect_process_detail" in tool_names
    assert "read_ops_file" in tool_names
    assert "read_klonet_logs" in tool_names
    assert "inspect_screen_session" in tool_names
    assert "inspect_nginx_routes" in tool_names
    assert "search_shared_ops_memory" in tool_names


def test_log_tool_schema_warns_about_source_and_historical_errors():
    from klonet_agent.tools.registry import TOOLS

    log_tool = next(
        item
        for item in TOOLS
        if item["function"]["name"] == "read_klonet_logs"
    )
    description = log_tool["function"]["description"]

    assert "resolved_path" in description
    assert "历史错误" in description
    assert "当前仍然故障" in description


def test_ops_profile_allows_screen_inspection():
    from klonet_agent.agents import get_profile

    profile = get_profile("ops")

    assert "render_klonet_config" in profile.allowed_tools
    assert "inspect_screen_session" in profile.allowed_tools
    assert "inspect_platform_instances" in profile.allowed_tools
    assert "inspect_ops_context" in profile.allowed_tools
    assert "inspect_process_detail" in profile.allowed_tools
    assert "inspect_nginx_routes" in profile.allowed_tools
    assert "read_ops_file" in profile.allowed_tools


def test_render_klonet_config_outputs_nginx_and_frontend_templates():
    from klonet_agent.tools.environment import render_klonet_config

    result = render_klonet_config(
        {
            "platform": "103",
            "server_name": "192.168.1.33",
            "master_port": 20220,
            "public_port": 20222,
            "terminal_port": 5045,
            "frontend_alias": "/VEMU2-103/",
            "frontend_path": "/home/adminis/lht/103_project/vemu_frontend/VEMU2",
        }
    )

    assert "render_klonet_config" in result
    assert "platform=103" in result
    assert "## nginx_server_block" in result
    assert "listen 20222;" in result
    assert "server_name 192.168.1.33;" in result
    assert "location /file/dload/" in result
    assert "proxy_pass http://127.0.0.1:20220/file/dload/;" in result
    assert "location /file/uload/" in result
    assert "location /reallyload/" in result
    assert "location /download/" in result
    assert "location / {" in result
    assert "proxy_pass http://127.0.0.1:20220;" in result
    assert "location /VEMU2-103/" in result
    assert "alias /home/adminis/lht/103_project/vemu_frontend/VEMU2/;" in result
    assert "## frontend_config_js" in result
    assert "192.168.1.33" in result
    assert "20222" in result
    assert "5045" in result
    assert "next_recipes=write_ops_file,reload_nginx" in result
    assert "environment unchanged" in result


def test_render_klonet_config_rejects_unsafe_inputs():
    from klonet_agent.tools.environment import render_klonet_config

    result = render_klonet_config(
        {
            "platform": "bad;name",
            "server_name": "example.com",
            "master_port": 70000,
            "public_port": 20222,
            "terminal_port": 5045,
            "frontend_alias": "VEMU2",
            "frontend_path": "/tmp/frontend",
        }
    )

    assert "render_klonet_config" in result
    assert "invalid_platform=bad;name" in result
    assert "environment unchanged" in result


def test_executor_dispatches_render_klonet_config_tool():
    from klonet_agent.tools.executor import ToolExecutor

    result = ToolExecutor(allowed_tools={"render_klonet_config"}).run(
        "render_klonet_config",
        {
            "platform": "103",
            "server_name": "localhost",
            "master_port": 12000,
            "public_port": 12002,
            "terminal_port": 12003,
            "frontend_alias": "/VEMU2/",
            "frontend_path": "/srv/vemu/VEMU2",
        },
    )

    assert "render_klonet_config" in result
    assert "listen 12002;" in result
    assert "proxy_pass http://127.0.0.1:12000;" in result


def test_inspect_nginx_routes_extracts_routes_from_config_file():
    from klonet_agent.tools.environment import inspect_nginx_routes
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        conf = temp_dir / "default.conf"
        conf.write_text(
            """
server {
    listen 20222;
    server_name 192.168.1.33;

    location /file/dload/ {
        proxy_pass http://127.0.0.1:20220/file/dload/;
    }

    location /VEMU2-103/ {
        alias /home/adminis/lht/103_project/vemu_frontend/VEMU2/;
    }
}
""",
            encoding="utf-8",
        )

        result = inspect_nginx_routes({"paths": [str(conf)]})

    assert "inspect_nginx_routes" in result
    assert f"source_path={conf}" in result
    assert "listen=20222" in result
    assert "server_name=192.168.1.33" in result
    assert "location=/file/dload/" in result
    assert "proxy_pass=http://127.0.0.1:20220/file/dload/" in result
    assert "location=/VEMU2-103/" in result
    assert "alias=/home/adminis/lht/103_project/vemu_frontend/VEMU2/" in result


def test_inspect_nginx_routes_rejects_sensitive_or_unsupported_paths():
    from klonet_agent.tools.environment import inspect_nginx_routes
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        env_file = temp_dir / ".env"
        env_file.write_text("SECRET=1\n", encoding="utf-8")

        result = inspect_nginx_routes({"paths": [str(env_file)]})

    assert "inspect_nginx_routes" in result
    assert "refused_sensitive_path=.env" in result


def test_executor_dispatches_inspect_nginx_routes_tool():
    from klonet_agent.tools.executor import ToolExecutor
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        conf = temp_dir / "nginx.conf"
        conf.write_text(
            "server { listen 12002; server_name localhost; location / { proxy_pass http://127.0.0.1:12000; } }",
            encoding="utf-8",
        )
        result = ToolExecutor(allowed_tools={"inspect_nginx_routes"}).run(
            "inspect_nginx_routes",
            {"paths": [str(conf)]},
        )

    assert "inspect_nginx_routes" in result
    assert "listen=12002" in result
    assert "proxy_pass=http://127.0.0.1:12000" in result


def test_ops_context_groups_baseline_runtime_and_assets(monkeypatch):
    from klonet_agent.tools import environment
    from tests.helpers import local_temp_dir

    def fake_probe(name):
        return environment.ProbeResult(name, "detected", f"{name}-detail")

    monkeypatch.setattr(environment, "run_read_only_probe", fake_probe)
    with local_temp_dir() as temp_dir:
        (temp_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (temp_dir / "Dockerfile").write_text("FROM python:3.8\n", encoding="utf-8")

        result = environment.inspect_ops_context(
            {
                "sections": ["baseline", "runtime", "assets"],
                "asset_roots": [str(temp_dir)],
            }
        )

    assert "inspect_ops_context" in result
    assert "## baseline" in result
    assert "os_release-detail" in result
    assert "docker_version-detail" in result
    assert "## runtime" in result
    assert "ports-detail" in result
    assert "docker_containers-detail" in result
    assert "## assets" in result
    assert "docker-compose.yml" in result
    assert "Dockerfile" in result


def test_platform_instance_inspection_groups_screens_processes_and_config(monkeypatch):
    from klonet_agent.tools import environment
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        project = temp_dir / "102_project"
        project.mkdir()
        (project / "config.py").write_text(
            "master_port = 12000\n"
            "worker_port = 12001\n"
            "public_port = 12002\n"
            "web_terminal_port = 5045\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            environment,
            "_screen_instance_rows",
            lambda: [
                {"session": "1024293.102_m", "platform": "102", "role": "master"},
                {"session": "1034358.102_c", "platform": "102", "role": "celery"},
                {"session": "1037323.102_w", "platform": "102", "role": "worker"},
                {"session": "1039800.102_web", "platform": "102", "role": "web_terminal"},
                {"session": "647892.lht_m", "platform": "lht", "role": "master"},
            ],
        )
        monkeypatch.setattr(
            environment,
            "_process_instance_rows",
            lambda: [
                {
                    "pid": 1467011,
                    "cwd": str(project),
                    "cmd": "sudo /usr/local/bin/gunicorn -c gun.py master_main:flask_app",
                    "platform": "102",
                    "role": "master",
                },
                {
                    "pid": 1467095,
                    "cwd": str(project),
                    "cmd": "python3.8 web_terminal_main.py",
                    "platform": "102",
                    "role": "web_terminal",
                },
            ],
        )

        result = environment.inspect_platform_instances(
            {"project_roots": [str(project)], "max_instances": 10}
        )

    assert "inspect_platform_instances" in result
    assert "platform=102" in result
    assert "roles=celery,master,web_terminal,worker" in result
    assert f"project_roots={project}" in result
    assert "ports=master_port:12000,worker_port:12001,public_port:12002,web_terminal_port:5045" in result
    assert "platform=lht" in result
    assert "source=screen" in result


def test_executor_persists_ops_baseline_snapshot():
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.tools.executor import ToolExecutor
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = MemoryStore.for_session(temp_dir / "memory", "u1", "p1")
        result = ToolExecutor(
            allowed_tools={"inspect_ops_context"},
            memory_store=store,
        ).run("inspect_ops_context", {"sections": ["baseline"]})

        baseline = store.read_shared_ops_baseline()

    assert "inspect_ops_context" in result
    assert "## baseline" in baseline
    assert "os_release" in baseline


def test_ops_file_reader_allows_config_and_redacts_secrets():
    from klonet_agent.tools.environment import read_ops_file
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        config = temp_dir / "config.py"
        config.write_text(
            "master_port = 12000\nredis_password = 'abc123'\n",
            encoding="utf-8",
        )

        result = read_ops_file({"path": str(config), "max_chars": 500})

    assert "read_ops_file" in result
    assert "resolved_path=" in result
    assert "master_port = 12000" in result
    assert "abc123" not in result
    assert "[REDACTED]" in result


def test_ops_file_reader_rejects_env_files():
    from klonet_agent.tools.environment import read_ops_file
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        env_file = temp_dir / ".env"
        env_file.write_text("OPENAI_API_KEY=secret", encoding="utf-8")

        result = read_ops_file({"path": str(env_file)})

    assert result.startswith("Error:")
    assert "refused" in result.lower()


def test_screen_inspection_rejects_unsafe_session_name():
    from klonet_agent.tools.environment import inspect_screen_session

    result = inspect_screen_session({"session": "102_m; rm -rf /"})

    assert result.startswith("Error:")
    assert "unsafe" in result.lower()


def test_screen_inspection_marks_scrollback_as_not_current_state(monkeypatch):
    from types import SimpleNamespace

    from klonet_agent.tools import environment
    from tests.helpers import local_temp_dir

    def fake_named_temp_file(**kwargs):
        class TempFile:
            name = str(snapshot)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        return TempFile()

    with local_temp_dir() as temp_dir:
        snapshot = temp_dir / "screen.log"
        snapshot.write_text("Traceback old error\nStarted!\n", encoding="utf-8")

        monkeypatch.setattr(environment.os, "name", "posix")
        monkeypatch.setattr(environment.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(environment.tempfile, "NamedTemporaryFile", fake_named_temp_file)
        monkeypatch.setattr(
            environment.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

        result = environment.inspect_screen_session({"session": "102_web", "max_chars": 200})

    assert "102_web: detected" in result
    assert "evidence_type=screen_scrollback" in result
    assert "current_state=false" in result
    assert "Traceback old error" in result


def test_runtime_probe_supports_process_cwd_evidence():
    """Ops diagnosis needs process cwd evidence before tying a platform to source."""

    from klonet_agent.tools.environment import _probe_command
    from klonet_agent.tools.registry import TOOLS

    command = _probe_command("processes")
    runtime_tool = next(
        item
        for item in TOOLS
        if item["function"]["name"] == "inspect_klonet_runtime"
    )
    checks = runtime_tool["function"]["parameters"]["properties"]["checks"]["items"]["enum"]

    assert command is not None
    assert "processes" in checks


def test_runtime_port_owner_returns_target_pid_cmd_and_cwd(monkeypatch):
    from types import SimpleNamespace

    from klonet_agent.tools import environment

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["ss", "-ltnp"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
                    "LISTEN 0 128 0.0.0.0:5045 0.0.0.0:* users:((\"python3.8\",pid=1467095,fd=7))\n"
                ),
                stderr="",
            )
        if command[:2] == ["ps", "-p"]:
            return SimpleNamespace(
                returncode=0,
                stdout="1467095 1467011 root python3.8 web_terminal_main.py\n",
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    monkeypatch.setattr(environment.os, "name", "posix")
    monkeypatch.setattr(environment.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        environment,
        "_read_proc_text",
        lambda path: "python3.8 web_terminal_main.py"
        if path.endswith("/cmdline")
        else "",
    )
    monkeypatch.setattr(
        environment,
        "_read_proc_link",
        lambda path: "/home/adminis/lht/102_project",
    )

    result = environment.inspect_klonet_runtime(
        {"checks": ["port_owner"], "ports": [5045]}
    )

    assert "port_owner: detected" in result
    assert "port=5045" in result
    assert "pid=1467095" in result
    assert "ppid=1467011" in result
    assert "user=root" in result
    assert "cmd=python3.8 web_terminal_main.py" in result
    assert "cwd=/home/adminis/lht/102_project" in result
    assert any(call[:2] == ["ss", "-ltnp"] for call in calls)


def test_process_detail_tool_returns_target_pid_cmd_and_cwd(monkeypatch):
    from types import SimpleNamespace

    from klonet_agent.tools import environment

    def fake_run(command, **kwargs):
        if command[:2] == ["ss", "-ltnp"]:
            return SimpleNamespace(
                returncode=0,
                stdout='LISTEN 0 128 0.0.0.0:5045 0.0.0.0:* users:(("python3.8",pid=1467095,fd=7))',
                stderr="",
            )
        if command[:2] == ["ps", "-p"]:
            return SimpleNamespace(
                returncode=0,
                stdout="1467095 1467011 root python3.8 web_terminal_main.py\n",
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    monkeypatch.setattr(environment.os, "name", "posix")
    monkeypatch.setattr(environment.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        environment,
        "_read_proc_text",
        lambda path: "python3.8 web_terminal_main.py"
        if path.endswith("/cmdline")
        else "",
    )
    monkeypatch.setattr(
        environment,
        "_read_proc_link",
        lambda path: "/home/adminis/lht/102_project",
    )

    result = environment.inspect_process_detail({"ports": [5045]})

    assert "inspect_process_detail" in result
    assert "port_owner: detected" in result
    assert "port=5045" in result
    assert "pid=1467095" in result
    assert "cmd=python3.8 web_terminal_main.py" in result
    assert "cwd=/home/adminis/lht/102_project" in result


def test_executor_dispatches_environment_tool():
    from klonet_agent.tools.executor import ToolExecutor

    result = ToolExecutor(allowed_tools={"inspect_system_environment"}).run(
        "inspect_system_environment",
        {"checks": ["os"]},
    )

    assert "inspect_system_environment" in result
    assert any(status in result for status in ("detected", "missing", "unchecked"))


def test_system_environment_can_probe_system_python_without_reading_binaries(monkeypatch):
    from types import SimpleNamespace

    from klonet_agent.tools import environment
    from klonet_agent.tools.registry import TOOLS

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout="/usr/bin/python3\nPython 3.8.10\nlrwxrwxrwx /usr/bin/python3 -> python3.8\n",
            stderr="",
        )

    monkeypatch.setattr(environment.os, "name", "posix")
    monkeypatch.setattr(environment.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(environment.subprocess, "run", fake_run)

    result = environment.inspect_system_environment({"checks": ["system_python"]})
    system_tool = next(
        item
        for item in TOOLS
        if item["function"]["name"] == "inspect_system_environment"
    )
    checks = system_tool["function"]["parameters"]["properties"]["checks"]["items"]["enum"]

    assert "system_python" in checks
    assert "system_python: detected" in result
    assert "/usr/bin/python3" in result
    assert "Python 3.8.10" in result
    assert any("/usr/bin/python3" in " ".join(call) for call in calls)


def test_executor_dispatches_process_detail_tool():
    from klonet_agent.tools.executor import ToolExecutor

    result = ToolExecutor(allowed_tools={"inspect_process_detail"}).run(
        "inspect_process_detail",
        {},
    )

    assert "inspect_process_detail" in result
    assert "process_detail: unchecked" in result


def test_executor_dispatches_screen_inspection_tool():
    from klonet_agent.tools.executor import ToolExecutor

    result = ToolExecutor(allowed_tools={"inspect_screen_session"}).run(
        "inspect_screen_session",
        {"session": "102_m; rm -rf /"},
    )

    assert result.startswith("Error:")
    assert "unsafe" in result.lower()
