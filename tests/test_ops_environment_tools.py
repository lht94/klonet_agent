"""Read-only environment diagnostic tool tests."""

import sys
from types import SimpleNamespace
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


def test_log_reader_honors_bounded_extended_tail_for_startup_tracebacks(tmp_path):
    from klonet_agent.tools.environment import read_klonet_logs

    marker = 'File "/srv/app/master_main.py", line 7, in <module>\nRuntimeError: boot\n'
    log_file = tmp_path / "master.log"
    log_file.write_text("x" * 3000 + marker + "y" * 12000, encoding="utf-8")

    default_tail = read_klonet_logs({"path": str(log_file)})
    extended_tail = read_klonet_logs({"path": str(log_file), "max_chars": 20000})

    assert marker not in default_tail
    assert marker in extended_tail
    assert "showing last 150" in extended_tail


def test_environment_tools_are_registered_for_llm():
    from klonet_agent.tools.registry import TOOLS

    tool_names = {item["function"]["name"] for item in TOOLS}

    assert "render_klonet_config" in tool_names
    assert "inspect_frontend_config" in tool_names
    assert "inspect_ops_context" in tool_names
    assert "inspect_platform_health" in tool_names
    assert "inspect_platform_instances" in tool_names
    assert "inspect_system_environment" in tool_names
    assert "inspect_service_health" in tool_names
    assert "inspect_klonet_runtime" in tool_names
    assert "inspect_process_detail" in tool_names
    assert "read_ops_file" in tool_names
    assert "read_klonet_logs" in tool_names
    assert "render_docker_daemon_config" in tool_names
    assert "inspect_archive" in tool_names
    assert "inspect_install_scripts" in tool_names
    assert "inspect_screen_session" in tool_names
    assert "inspect_nginx_routes" in tool_names
    assert "search_shared_ops_memory" in tool_names


def test_render_klonet_config_schema_exposes_optional_frontend_config_path():
    from klonet_agent.tools.registry import TOOLS

    tool = next(item for item in TOOLS if item["function"]["name"] == "render_klonet_config")
    params = tool["function"]["parameters"]

    assert "frontend_config_path" in params["properties"]
    assert "frontend_config_path" not in params["required"]


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
    assert "inspect_frontend_config" in profile.allowed_tools
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
            "worker_port": 20221,
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
    assert "## backend_config_py" in result
    assert "master_port = 20220" in result
    assert "worker_port = 20221" in result
    assert "public_port = 20222" in result
    assert "web_terminal_port = 5045" in result
    assert "## web_terminal_main_py_patch_hint" in result
    assert "WSGIServer(('0.0.0.0', 5045)" in result
    assert "## frontend_config_js" in result
    assert "192.168.1.33" in result
    assert "20222" in result
    assert "5045" in result
    assert "next_recipes=write_ops_file,reload_nginx" in result
    assert "environment unchanged" in result


def test_render_klonet_config_aligns_existing_frontend_config_fields():
    from klonet_agent.tools.environment import render_klonet_config
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        config = temp_dir / "config.js"
        config.write_text(
            "\n".join(
                [
                    'var server_ip = "10.0.0.1";',
                    "var server_port = 10000;",
                    "var terminal_port = 10001;",
                    "var keep_me = true;",
                ]
            ),
            encoding="utf-8",
        )

        result = render_klonet_config(
            {
                "platform": "103",
                "server_name": "192.168.1.33",
                "master_port": 20220,
                "worker_port": 20221,
                "public_port": 20222,
                "terminal_port": 5045,
                "frontend_alias": "/VEMU2-103/",
                "frontend_path": "/home/adminis/lht/103_project/vemu_frontend/VEMU2",
                "frontend_config_path": str(config),
            }
        )

    assert f"frontend_config_source={config.resolve()}" in result
    assert "## frontend_config_js_patch_draft" in result
    assert 'var server_ip = "192.168.1.33";' in result
    assert "var server_port = 20222;" in result
    assert "var terminal_port = 5045;" in result
    assert "var keep_me = true;" in result
    assert "var backend_ip" not in result
    assert "environment unchanged" in result


def test_render_klonet_config_rejects_sensitive_frontend_config_path():
    from klonet_agent.tools.environment import render_klonet_config
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        secret = temp_dir / ".env"
        secret.write_text("OPENAI_API_KEY=secret", encoding="utf-8")

        result = render_klonet_config(
            {
                "platform": "103",
                "server_name": "192.168.1.33",
                "master_port": 20220,
                "worker_port": 20221,
                "public_port": 20222,
                "terminal_port": 5045,
                "frontend_alias": "/VEMU2-103/",
                "frontend_path": "/home/adminis/lht/103_project/vemu_frontend/VEMU2",
                "frontend_config_path": str(secret),
            }
        )

    assert "render_klonet_config" in result
    assert "refused_sensitive_path=.env" in result
    assert "environment unchanged" in result


def test_render_klonet_config_rejects_unsafe_inputs():
    from klonet_agent.tools.environment import render_klonet_config

    result = render_klonet_config(
        {
            "platform": "bad;name",
            "server_name": "example.com",
            "master_port": 70000,
            "worker_port": 20221,
            "public_port": 20222,
            "terminal_port": 5045,
            "frontend_alias": "VEMU2",
            "frontend_path": "/tmp/frontend",
        }
    )

    assert "render_klonet_config" in result
    assert "invalid_platform=bad;name" in result
    assert "environment unchanged" in result


def test_frontend_config_validator_confirms_fields_and_nginx_alias():
    from klonet_agent.tools.environment import inspect_frontend_config
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        config = temp_dir / "config.js"
        config.write_text(
            "\n".join(
                [
                    'var server_ip = "192.168.1.33";',
                    "var server_port = 20222;",
                    "var terminal_port = 5045;",
                ]
            ),
            encoding="utf-8",
        )
        nginx = temp_dir / "nginx.conf"
        nginx.write_text(
            "server { listen 20222; location /VEMU2-103/ { alias /home/adminis/lht/103_project/vemu_frontend/VEMU2/; } }",
            encoding="utf-8",
        )

        result = inspect_frontend_config(
            {
                "frontend_config_path": str(config),
                "server_name": "192.168.1.33",
                "public_port": 20222,
                "terminal_port": 5045,
                "frontend_alias": "/VEMU2-103/",
                "frontend_path": "/home/adminis/lht/103_project/vemu_frontend/VEMU2",
                "nginx_paths": [str(nginx)],
            }
        )

    assert "inspect_frontend_config" in result
    assert f"frontend_config_source={config.resolve()}" in result
    assert "field=server_ip actual=192.168.1.33 expected=192.168.1.33 status=matched" in result
    assert "field=server_port actual=20222 expected=20222 status=matched" in result
    assert "field=terminal_port actual=5045 expected=5045 status=matched" in result
    assert "frontend_config_status=aligned" in result
    assert "nginx_alias_status=matched" in result
    assert "overall_status=ready" in result
    assert "environment unchanged" in result


def test_frontend_config_validator_blocks_mismatched_port():
    from klonet_agent.tools.environment import inspect_frontend_config
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        config = temp_dir / "config.js"
        config.write_text(
            "\n".join(
                [
                    'var backend_ip = "192.168.1.33";',
                    "var backend_port = 30000;",
                    "var web_terminal_port = 5045;",
                ]
            ),
            encoding="utf-8",
        )

        result = inspect_frontend_config(
            {
                "frontend_config_path": str(config),
                "server_name": "192.168.1.33",
                "public_port": 20222,
                "terminal_port": 5045,
            }
        )

    assert "inspect_frontend_config" in result
    assert "field=backend_port actual=30000 expected=20222 status=mismatch" in result
    assert "frontend_config_status=blocked" in result
    assert "overall_status=blocked" in result


def test_executor_dispatches_frontend_config_validator():
    from klonet_agent.tools.executor import ToolExecutor

    result = ToolExecutor(allowed_tools={"inspect_frontend_config"}).run(
        "inspect_frontend_config",
        {"frontend_config_path": "/tmp/missing.js", "server_name": "127.0.0.1", "public_port": 20222},
    )

    assert "inspect_frontend_config" in result
    assert "frontend_config_status=missing" in result


def test_executor_dispatches_render_klonet_config_tool():
    from klonet_agent.tools.executor import ToolExecutor

    result = ToolExecutor(allowed_tools={"render_klonet_config"}).run(
        "render_klonet_config",
        {
            "platform": "103",
            "server_name": "localhost",
            "master_port": 12000,
            "worker_port": 12001,
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


def test_archive_inspection_lists_members_without_extracting():
    import zipfile

    from klonet_agent.tools.environment import inspect_archive
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        archive = temp_dir / "vemu_install_new_gen.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("vemu_install_new_gen/base_requ_setup.sh", "echo setup")
            handle.writestr("vemu_install_new_gen/docker_service.sh", "echo docker")

        result = inspect_archive({"path": str(archive), "max_members": 10})

    assert "inspect_archive" in result
    assert "archive_type=zip" in result
    assert "member_count=2" in result
    assert "vemu_install_new_gen/base_requ_setup.sh" in result
    assert "unsafe_members=none" in result
    assert "environment unchanged" in result
    assert not (temp_dir / "vemu_install_new_gen").exists()


def test_archive_inspection_reports_path_traversal_members():
    import zipfile

    from klonet_agent.tools.environment import inspect_archive
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        archive = temp_dir / "bad.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("../escape.sh", "echo bad")

        result = inspect_archive({"path": str(archive)})

    assert "inspect_archive" in result
    assert "unsafe_members=../escape.sh" in result
    assert "environment unchanged" in result


def test_executor_dispatches_archive_inspection_tool():
    from klonet_agent.tools.executor import ToolExecutor

    result = ToolExecutor(allowed_tools={"inspect_archive"}).run(
        "inspect_archive",
        {"path": "/tmp/missing.zip"},
    )

    assert "inspect_archive" in result
    assert "archive_missing=/tmp/missing.zip" in result


def test_render_docker_daemon_config_merges_insecure_registry_without_overwriting():
    import json

    from klonet_agent.tools.environment import render_docker_daemon_config
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        daemon = temp_dir / "daemon.json"
        daemon.write_text(
            json.dumps(
                {
                    "registry-mirrors": ["https://mirror.example"],
                    "dns": ["8.8.8.8"],
                    "runtimes": {
                        "nvidia": {
                            "path": "nvidia-container-runtime",
                            "runtimeArgs": [],
                        }
                    },
                    "insecure-registries": ["192.168.1.10:5024"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        result = render_docker_daemon_config(
            {
                "path": str(daemon),
                "registry": "192.168.1.33:5024",
            }
        )

    assert "render_docker_daemon_config" in result
    assert f"source_path={daemon.resolve()}" in result
    assert "template_status=draft" in result
    assert "environment unchanged" in result
    assert "next_recipes=write_ops_file" in result
    assert '"registry-mirrors": [' in result
    assert '"https://mirror.example"' in result
    assert '"dns": [' in result
    assert '"8.8.8.8"' in result
    assert '"runtimes": {' in result
    assert '"192.168.1.10:5024"' in result
    assert '"192.168.1.33:5024"' in result


def test_render_docker_daemon_config_missing_file_uses_minimal_draft():
    from klonet_agent.tools.environment import render_docker_daemon_config
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        daemon = temp_dir / "daemon.json"
        result = render_docker_daemon_config(
            {
                "path": str(daemon),
                "registry": "10.0.0.5:5024",
            }
        )

    assert "render_docker_daemon_config" in result
    assert f"source_path={daemon.resolve()}" in result
    assert "source_status=missing" in result
    assert '"insecure-registries": [' in result
    assert '"10.0.0.5:5024"' in result
    assert "environment unchanged" in result


def test_service_health_summary_recommends_reusing_running_services(monkeypatch):
    from klonet_agent.tools import environment

    fake_results = {
        "docker_containers": environment.ProbeResult(
            "docker_containers",
            "detected",
            "redis-celery Up 6 days | mysql-vemu Up 6 days | rabbitmq-server Up 6 days",
        ),
        "nginx": environment.ProbeResult("nginx", "detected", "active"),
        "redis": environment.ProbeResult("redis", "detected", "active"),
        "mysql": environment.ProbeResult("mysql", "detected", "active"),
        "rabbitmq": environment.ProbeResult("rabbitmq", "detected", "active"),
    }

    monkeypatch.setattr(environment, "run_read_only_probe", lambda name: fake_results[name])

    result = environment.inspect_service_health(
        {"services": ["redis", "mysql", "rabbitmq", "nginx", "docker_containers"]}
    )

    assert "inspect_service_health" in result
    assert "service=redis status=detected recommendation=reuse" in result
    assert "service=mysql status=detected recommendation=reuse" in result
    assert "service=rabbitmq status=detected recommendation=reuse" in result
    assert "service=nginx status=detected recommendation=reuse" in result
    assert "service=docker_containers status=detected recommendation=reuse" in result
    assert "docker_service_action=skip" in result
    assert "environment unchanged" in result


def test_service_health_summary_marks_missing_services_as_start_candidate(monkeypatch):
    from klonet_agent.tools import environment

    fake_results = {
        "redis": environment.ProbeResult("redis", "missing", "inactive"),
        "nginx": environment.ProbeResult("nginx", "unchecked", "systemctl not found"),
    }

    monkeypatch.setattr(environment, "run_read_only_probe", lambda name: fake_results[name])

    result = environment.inspect_service_health({"services": ["redis", "nginx"]})

    assert "service=redis status=missing recommendation=start_candidate" in result
    assert "service=nginx status=unchecked recommendation=inspect" in result
    assert "docker_service_action=inspect_before_run" in result


def test_executor_dispatches_service_health_tool():
    from klonet_agent.tools.executor import ToolExecutor

    result = ToolExecutor(allowed_tools={"inspect_service_health"}).run(
        "inspect_service_health",
        {"services": []},
    )

    assert "inspect_service_health" in result
    assert "environment unchanged" in result


def test_platform_health_verifier_summarizes_config_screen_process_ports_and_nginx(monkeypatch):
    from klonet_agent.tools import environment
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        project_root = temp_dir / "103_project"
        project_root.mkdir()
        config = project_root / "config.py"
        config.write_text(
            "master_port = 20220\n"
            "worker_port = 20221\n"
            "public_port = 20222\n"
            "web_terminal_port = 5045\n",
            encoding="utf-8",
        )
        nginx_conf = temp_dir / "nginx.conf"
        nginx_conf.write_text(
            "server { listen 20222; location /VEMU2-103/ { alias /srv/frontend/; } }",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            environment,
            "_screen_instance_rows",
            lambda: [
                {"platform": "103", "role": "master", "session": "103_m"},
                {"platform": "103", "role": "worker", "session": "103_w"},
                {"platform": "103", "role": "celery", "session": "103_c"},
                {"platform": "103", "role": "web_terminal", "session": "103_web"},
            ],
        )
        monkeypatch.setattr(
            environment,
            "_process_instance_rows",
            lambda: [
                {"platform": "103", "role": "master", "pid": 1101, "cwd": str(project_root)},
                {"platform": "103", "role": "worker", "pid": 1102, "cwd": str(project_root)},
                {"platform": "103", "role": "celery", "pid": 1103, "cwd": str(project_root)},
                {"platform": "103", "role": "web_terminal", "pid": 1104, "cwd": str(project_root)},
            ],
        )
        monkeypatch.setattr(
            environment,
            "_inspect_port_owners",
            lambda args: [
                environment.ProbeResult("port_owner", "detected", f"port={port} pid={2000 + port}")
                for port in args["ports"]
            ],
        )

        result = environment.inspect_platform_health(
            {
                "platform": "103",
                "project_root": str(project_root),
                "nginx_paths": [str(nginx_conf)],
            }
        )

    assert "inspect_platform_health" in result
    assert "platform=103" in result
    assert f"project_root={project_root}" in result
    assert "screen_status=ready roles=celery,master,web_terminal,worker" in result
    assert "process_status=ready roles=celery,master,web_terminal,worker" in result
    assert "config_ports=master_port:20220,worker_port:20221,public_port:20222,web_terminal_port:5045" in result
    assert "port_status=ready ports=5045,20220,20221,20222" in result
    assert "nginx_status=detected" in result
    assert "overall_status=ready" in result
    assert "environment unchanged" in result


def test_platform_health_verifier_blocks_missing_configured_port(monkeypatch):
    from klonet_agent.tools import environment
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        project_root = temp_dir / "103_project"
        project_root.mkdir()
        (project_root / "config.py").write_text("master_port = 20220\n", encoding="utf-8")

        monkeypatch.setattr(environment, "_screen_instance_rows", lambda: [])
        monkeypatch.setattr(environment, "_process_instance_rows", lambda: [])
        monkeypatch.setattr(
            environment,
            "_inspect_port_owners",
            lambda args: [environment.ProbeResult("port_owner", "missing", "port=20220 not listening")],
        )

        result = environment.inspect_platform_health({"platform": "103", "project_root": str(project_root)})

    assert "inspect_platform_health" in result
    assert "screen_status=missing" in result
    assert "process_status=missing" in result
    assert "port_status=blocked" in result
    assert "port=20220 not listening" in result
    assert "overall_status=blocked" in result


def test_platform_health_reads_ports_from_active_vemu_config_class(monkeypatch):
    from klonet_agent.tools import environment
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        project_root = temp_dir / "lht_project"
        config_dir = project_root / "vemu_config"
        config_dir.mkdir(parents=True)
        (config_dir / "config.py").write_text(
            "\n".join(
                [
                    "class CommonConfig:",
                    "    master_port = 12000",
                    "    worker_port = 12001",
                    "    public_port = '80'",
                    "class LhtConfig(CommonConfig):",
                    "    master_port = 27700",
                    "    worker_port = 27701",
                    "    web_terminal_port = 27702",
                    "    public_port = 8380",
                    "PROJ_CONFIG = LhtConfig()",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            environment,
            "_screen_instance_rows",
            lambda: [
                {"platform": "lht", "role": "master", "session": "lht_m"},
                {"platform": "lht", "role": "worker", "session": "lht_w"},
                {"platform": "lht", "role": "celery", "session": "lht_c"},
                {"platform": "lht", "role": "web_terminal", "session": "lht_web"},
            ],
        )
        monkeypatch.setattr(
            environment,
            "_process_instance_rows",
            lambda: [
                {"platform": "lht", "role": "master", "pid": 1, "cwd": str(project_root)},
                {"platform": "lht", "role": "worker", "pid": 2, "cwd": str(project_root)},
                {"platform": "lht", "role": "celery", "pid": 3, "cwd": str(project_root)},
                {"platform": "lht", "role": "web_terminal", "pid": 4, "cwd": str(project_root)},
            ],
        )
        monkeypatch.setattr(
            environment,
            "_inspect_port_owners",
            lambda args: [
                environment.ProbeResult("port_owner", "detected", f"port={port} pid={port}")
                for port in args["ports"]
            ],
        )

        result = environment.inspect_platform_health({"platform": "lht", "project_root": str(project_root)})

    assert "config_ports=master_port:27700,worker_port:27701,public_port:8380,web_terminal_port:27702" in result
    assert "port_status=ready ports=8380,27700,27701,27702" in result
    assert "overall_status=unchecked" in result


def test_runtime_role_listener_binding_distinguishes_code_root_from_runtime_root(
    monkeypatch, tmp_path,
):
    from klonet_agent.tools import environment

    project_root = tmp_path / "vemu_uestc"
    project_root.mkdir()
    external_root = tmp_path / "simulation" / "worker101"
    external_root.mkdir(parents=True)
    monkeypatch.setattr(
        environment,
        "_port_owner_result",
        lambda _port: environment.ProbeResult(
            "port_owner", "detected", "port=45552 pid=220 tree_root_pid=220",
        ),
    )
    monkeypatch.setattr(
        environment,
        "_process_detail",
        lambda _pid: {
            "pgid": "220",
            "cwd": str(external_root),
            "cmd": (
                "/opt/env/bin/python3.8 -m gunicorn --pid "
                "/srv/docker simulation/worker101/gunicorn.pid -c %s "
                "worker_main:flask_app"
            )
            % (project_root / "mains" / "worker_gun.py"),
        },
    )

    binding = environment._runtime_role_listener_binding(
        project_root, "worker", 45552,
    )

    assert binding["status"] == "runtime_conflict"
    assert binding["listener_pid"] == 220
    assert binding["listener_pgid"] == 220
    assert binding["runtime_root"] == str(external_root)
    assert binding["code_root"] == str(project_root)


def test_runtime_root_never_falls_back_to_imported_code_path():
    from klonet_agent.tools.environment import _runtime_root_from_process_row

    row = {
        "cwd": "?",
        "cmd": (
            "python -m gunicorn --chdir /host/home/lzl "
            "-c /home/lzl/vemu_uestc/mains/worker_gun.py "
            "vemu_uestc.mains.worker_main:flask_app"
        ),
    }

    assert _runtime_root_from_process_row(row) == ""


def test_runtime_root_accepts_explicit_screen_cd_contract():
    from klonet_agent.tools.environment import _runtime_root_from_process_row

    row = {
        "cwd": "?",
        "cmd": "SCREEN -dmS alpha_w bash -lc cd /srv/alpha/mains && exec python",
    }

    assert _runtime_root_from_process_row(row) == "/srv/alpha"


def test_runtime_role_listener_binding_confirms_project_mains_cwd(
    monkeypatch, tmp_path,
):
    from klonet_agent.tools import environment

    project_root = tmp_path / "vemu_uestc"
    (project_root / "mains").mkdir(parents=True)
    monkeypatch.setattr(
        environment,
        "_port_owner_result",
        lambda _port: environment.ProbeResult(
            "port_owner", "detected", "port=45552 pid=330",
        ),
    )
    monkeypatch.setattr(
        environment,
        "_process_detail",
        lambda _pid: {
            "pgid": "329",
            "cwd": str(project_root / "mains"),
            "cmd": "gunicorn -c worker_gun.py worker_main:flask_app",
        },
    )

    binding = environment._runtime_role_listener_binding(
        project_root, "worker", 45552,
    )

    assert binding["status"] == "confirmed"
    assert binding["listener_pid"] == 330
    assert binding["listener_pgid"] == 329


def test_executor_dispatches_platform_health_tool():
    from klonet_agent.tools.executor import ToolExecutor

    result = ToolExecutor(allowed_tools={"inspect_platform_health"}).run(
        "inspect_platform_health",
        {"platform": "103", "project_root": "/tmp/missing"},
    )

    assert "inspect_platform_health" in result
    assert "project_root_status=missing" in result


def test_install_script_inspection_reports_allowed_scripts_and_risks():
    from klonet_agent.tools.environment import inspect_install_scripts
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        install_dir = temp_dir / "vemu_install_new_gen"
        install_dir.mkdir()
        setup = install_dir / "base_requ_setup.sh"
        setup.write_text(
            "#!/usr/bin/env bash\n"
            "apt-get update\n"
            "systemctl restart docker\n",
            encoding="utf-8",
        )
        docker = install_dir / "docker_service.sh"
        docker.write_text(
            "#!/bin/bash\n"
            "docker run --name redis-celery redis\n",
            encoding="utf-8",
        )

        result = inspect_install_scripts({"script_dir": str(install_dir)})

    assert "inspect_install_scripts" in result
    assert "script=base_requ_setup.sh status=detected" in result
    assert "script=docker_service.sh status=detected" in result
    assert "shebang=#!/usr/bin/env bash" in result
    assert "recommended_recipe=run_install_script" in result
    assert "allowed_args=NORMAL" in result
    assert "risk_markers=apt-get,systemctl" in result
    assert "risk_markers=docker" in result
    assert "environment unchanged" in result


def test_install_script_inspection_blocks_missing_required_script():
    from klonet_agent.tools.environment import inspect_install_scripts
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        install_dir = temp_dir / "vemu_install_new_gen"
        install_dir.mkdir()
        (install_dir / "base_requ_setup.sh").write_text("# setup\n", encoding="utf-8")

        result = inspect_install_scripts({"script_dir": str(install_dir)})

    assert "inspect_install_scripts" in result
    assert "script=docker_service.sh status=missing" in result
    assert "preflight_status=blocked" in result
    assert "environment unchanged" in result


def test_executor_dispatches_install_script_inspection_tool():
    from klonet_agent.tools.executor import ToolExecutor

    result = ToolExecutor(allowed_tools={"inspect_install_scripts"}).run(
        "inspect_install_scripts",
        {"script_dir": "/tmp/missing"},
    )

    assert "inspect_install_scripts" in result
    assert "script_dir_missing=/tmp/missing" in result


def test_executor_dispatches_docker_daemon_config_renderer():
    from klonet_agent.tools.executor import ToolExecutor
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        daemon = temp_dir / "daemon.json"
        result = ToolExecutor(allowed_tools={"render_docker_daemon_config"}).run(
            "render_docker_daemon_config",
            {"path": str(daemon), "registry": "10.0.0.5:5024"},
        )

    assert "render_docker_daemon_config" in result
    assert '"10.0.0.5:5024"' in result


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


def test_platform_instance_inspection_counts_runtime_roots_without_duplicate_aliases(monkeypatch):
    from klonet_agent.tools import environment
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        target_root = temp_dir / "klonet_workflow_e2e"
        source_root = temp_dir / "vemu_uestc"
        (target_root / "mains").mkdir(parents=True)
        (source_root / "mains").mkdir(parents=True)
        monkeypatch.setattr(
            environment,
            "_screen_instance_rows",
            lambda: [
                {"session": "1.v4e2e_m", "platform": "v4e2e", "role": "master"},
                {"session": "2.vemu_uestc_m", "platform": "vemu_uestc", "role": "master"},
            ],
        )
        monkeypatch.setattr(
            environment,
            "_process_instance_rows",
            lambda: [
                {
                    "pid": 1,
                    "cwd": str(target_root / "mains"),
                    "cmd": "SCREEN -dmS v4e2e_m bash -lc run",
                    "platform": "mains",
                    "role": "master",
                },
                {
                    "pid": 2,
                    "cwd": str(target_root / "mains"),
                    "cmd": "python -m gunicorn -c gun.py master_main:flask_app",
                    "platform": "mains",
                    "role": "master",
                },
                {
                    "pid": 3,
                    "cwd": str(source_root / "mains"),
                    "cmd": "SCREEN -dmS vemu_uestc_m bash -lc run",
                    "platform": "mains",
                    "role": "master",
                },
                {
                    "pid": 4,
                    "cwd": str(temp_dir / "agent-worktree"),
                    "cmd": "python -m klonet_agent.agent",
                    "platform": "agent-worktree",
                    "role": "unknown",
                },
                {
                    "pid": 5,
                    "cwd": "?",
                    "cmd": "python -m celery -A mains.celery_worker:celery worker",
                    "platform": "unknown",
                    "role": "celery",
                },
            ],
        )

        result = environment.inspect_platform_instances({"max_instances": 10})

    assert "instance_count=2" in result
    assert "platform=mains" not in result
    assert "platform=agent-worktree" not in result
    assert "unresolved_process_evidence=roles:celery pids:5" in result
    assert result.count("platform=v4e2e") == 1
    assert result.count("platform=vemu_uestc") == 1
    assert f"project_roots={target_root}" in result
    assert f"project_roots={source_root}" in result


def test_running_platform_inspection_counts_only_backend_healthy_runtime_roots(monkeypatch):
    from klonet_agent.tools import environment
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        healthy_root = temp_dir / "vemu_uestc"
        partial_root = temp_dir / "test" / "vemu_uestc"
        code_only_root = temp_dir / "code_only"
        for root, master_port, worker_port in (
            (healthy_root, 30101, 30102),
            (partial_root, 30201, 30202),
            (code_only_root, 30301, 30302),
        ):
            config_dir = root / "vemu_config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.py").write_text(
                "class ActiveConfig:\n"
                f"    master_port = {master_port}\n"
                f"    worker_port = {worker_port}\n"
                "    public_port = 39999\n"
                "PROJ_CONFIG = ActiveConfig()\n",
                encoding="utf-8",
            )

        monkeypatch.setattr(
            environment,
            "_screen_instance_rows",
            lambda: [
                {
                    "session": "1.vemu_uestc_m", "platform": "vemu_uestc",
                    "role": "master", "project_root": str(healthy_root),
                },
                {
                    "session": "2.test_m", "platform": "test",
                    "role": "master", "project_root": str(partial_root),
                },
            ],
        )
        monkeypatch.setattr(
            environment,
            "_process_instance_rows",
            lambda: [
                {
                    "pid": 1,
                    "uid": 997,
                    "executable": "/usr/bin/python3.8",
                    "cwd": str(healthy_root / "mains"),
                    "cmd": "python -m gunicorn -c gun.py master_main:flask_app",
                    "platform": "vemu_uestc",
                    "role": "master",
                },
                {
                    "pid": 2,
                    "uid": 998,
                    "executable": "/opt/worker/bin/python3.8",
                    "cwd": str(healthy_root),
                    "cmd": "python -m gunicorn -c worker_gun.py worker_main:flask_app",
                    "platform": "vemu_uestc",
                    "role": "worker",
                },
                {
                    "pid": 4,
                    "uid": 996,
                    "executable": "/opt/celery/bin/python3.8",
                    "cwd": str(healthy_root),
                    "cmd": "python -m celery -A celery_worker.celery worker",
                    "platform": "vemu_uestc",
                    "role": "celery",
                },
                {
                    "pid": 3,
                    "cwd": str(partial_root),
                    "cmd": "python -m gunicorn -c gun.py master_main:flask_app",
                    "platform": "vemu_uestc",
                    "role": "master",
                },
            ],
        )
        monkeypatch.setattr(
            environment,
            "_probe_backend_endpoint",
            lambda port: {
                30101: ("healthy", 200, "code=1"),
                30102: ("healthy", 200, "code=1"),
                30201: ("timeout", 0, "TimeoutError"),
            }.get(port, ("not_checked", 0, "role_not_running")),
        )

        result = environment.inspect_running_platforms(
            {"project_roots": [str(code_only_root)]}
        )

    assert "inspect_running_platforms" in result
    assert "runtime_candidate_count=2" in result
    assert "healthy_count=1" in result
    assert "abnormal_count=1" in result
    assert "code_only_count=1" in result
    assert result.count("platform=vemu_uestc") == 1
    assert result.count("platform=test ") == 1
    assert f"project_root={healthy_root}" in result
    assert "backend_status=healthy" in result
    assert "master_endpoint=healthy http_status=200" in result
    assert "worker_endpoint=healthy http_status=200" in result
    assert f"project_root={partial_root}" in result
    assert "backend_status=abnormal" in result
    assert "missing_roles=worker" in result
    assert "worker_port=30202 worker_endpoint=not_checked reason=role_not_running" in result
    assert f"code_only_root={code_only_root}" in result
    assert "configured_ports=master_port:30101,worker_port:30102" in result
    assert "master_identities=1:997:/usr/bin/python3.8" in result
    assert "worker_identities=2:998:/opt/worker/bin/python3.8" in result
    assert "celery_identities=4:996:/opt/celery/bin/python3.8" in result
    assert "web_terminal_identities=none" in result
    assert "runtime_identities=1:997:/usr/bin/python3.8" in result
    assert "screen_sessions_b64=" in result


def test_colliding_runtime_basenames_are_deterministically_path_qualified():
    from klonet_agent.tools.environment import _qualify_colliding_platform_names

    instances = {
        "/srv/vemu_uestc": {"platform": "vemu_uestc"},
        "/srv/lab/vemu_uestc": {"platform": "vemu_uestc"},
    }

    _qualify_colliding_platform_names(instances, {})

    assert instances["/srv/vemu_uestc"]["platform"] == "vemu_uestc"
    assert instances["/srv/lab/vemu_uestc"]["platform"] == "lab_vemu_uestc"


def test_running_platforms_keeps_unqualified_process_root_as_external_evidence(
    monkeypatch, tmp_path,
):
    from klonet_agent.tools import environment

    platform_root = tmp_path / "platform" / "vemu_uestc"
    external_root = tmp_path / "simulation" / "worker101"
    platform_root.mkdir(parents=True)
    external_root.mkdir(parents=True)
    monkeypatch.setattr(
        environment,
        "_screen_instance_rows",
        lambda: [{
            "session": "10.vemu_m", "platform": "vemu", "role": "master",
            "project_root": str(platform_root),
        }],
    )
    monkeypatch.setattr(
        environment,
        "_process_instance_rows",
        lambda: [
            {
                "pid": 10, "uid": 1000, "pgid": 10,
                "executable": "/opt/python3.8", "cwd": str(platform_root),
                "cmd": "python -m gunicorn -c gun.py master_main:flask_app",
                "platform": "vemu", "role": "master",
            },
            {
                "pid": 20, "uid": 0, "pgid": 20,
                "executable": "/opt/python3.8", "cwd": str(external_root),
                "cmd": (
                    "python -m gunicorn -c %s worker_main:flask_app"
                    % (platform_root / "mains" / "worker_gun.py")
                ),
                "platform": "worker101", "role": "worker",
            },
        ],
    )
    monkeypatch.setattr(environment, "_discover_klonet_code_roots", lambda _roots: [])

    result = environment.inspect_running_platforms()

    assert "runtime_candidate_count=1" in result
    assert "platform=vemu project_root=%s" % platform_root in result
    assert "platform=worker101" not in result
    assert "external_runtime_count=1" in result
    assert (
        "external_runtime_root=%s classification=conflict_evidence_only"
        % external_root
    ) in result


def test_platform_instances_does_not_promote_unqualified_process_cwd(
    monkeypatch, tmp_path,
):
    from klonet_agent.tools import environment

    platform_root = tmp_path / "platform" / "vemu_uestc"
    external_root = tmp_path / "simulation" / "worker101"
    platform_root.mkdir(parents=True)
    external_root.mkdir(parents=True)
    monkeypatch.setattr(
        environment,
        "_screen_instance_rows",
        lambda: [{
            "session": "10.vemu_m", "platform": "vemu", "role": "master",
            "project_root": str(platform_root),
        }],
    )
    monkeypatch.setattr(
        environment,
        "_process_instance_rows",
        lambda: [
            {
                "pid": 10, "cwd": str(platform_root), "cmd": "master_main",
                "platform": "vemu", "role": "master",
            },
            {
                "pid": 20, "cwd": str(external_root), "cmd": "worker_main",
                "platform": "worker101", "role": "worker",
            },
        ],
    )

    result = environment.inspect_platform_instances()

    assert "instance_count=1" in result
    assert "platform=vemu" in result
    assert "platform=worker101" not in result
    assert "unresolved_process_evidence=roles:worker pids:20" in result


def test_unique_runtime_platform_name_is_not_rewritten():
    from klonet_agent.tools.environment import _qualify_colliding_platform_names

    instances = {
        "/srv/alpha": {"platform": "alpha"},
        "/srv/beta": {"platform": "beta"},
    }

    _qualify_colliding_platform_names(instances, {})

    assert instances == {
        "/srv/alpha": {"platform": "alpha"},
        "/srv/beta": {"platform": "beta"},
    }


def test_runtime_http_transport_bypasses_ambient_proxy_for_all_loopback_forms(
    monkeypatch,
):
    from klonet_agent.tools import environment

    opened = []

    class Response:
        status = 200

    class Opener:
        def open(self, request, *, timeout):
            opened.append((request, timeout))
            return Response()

    handlers = []

    def build_opener(*items):
        handlers.extend(items)
        return Opener()

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setattr(environment.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(
        environment.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("loopback request used ambient proxy path")
        ),
    )

    for url in (
        "http://127.0.0.1:47001/server_health/",
        "http://127.9.8.7:47002/server_health/",
        "http://localhost:45551/server_health/",
        "http://[::1]:45552/server_health/",
    ):
        assert environment.http_transport_for_url(url) == "direct_loopback"
        assert environment.open_http_request(url, timeout=3).status == 200

    assert len(opened) == 4
    assert len(handlers) == 4
    assert all(handler.proxies == {} for handler in handlers)


def test_runtime_http_transport_preserves_default_proxy_path_for_remote_url(
    monkeypatch,
):
    from klonet_agent.tools import environment

    calls = []

    class Response:
        status = 204

    def urlopen(request, *, timeout):
        calls.append((request, timeout))
        return Response()

    monkeypatch.setattr(environment.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        environment.urllib.request,
        "build_opener",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("remote request unexpectedly bypassed proxy policy")
        ),
    )

    url = "https://health.example.invalid/status"
    assert environment.http_transport_for_url(url) == "default"
    assert environment.open_http_request(url, timeout=7).status == 204
    assert calls == [(url, 7)]


def test_running_platform_inspection_uses_screen_alias_for_runtime_root(monkeypatch):
    from klonet_agent.tools import environment
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        root = temp_dir / "klonet_workflow_e2e"
        (root / "mains").mkdir(parents=True)
        monkeypatch.setattr(
            environment,
            "_process_instance_rows",
            lambda: [
                {
                    "pid": 10,
                    "cwd": str(root / "mains"),
                    "cmd": "SCREEN -dmS v4e2e_m bash -lc run master_main",
                    "platform": "mains",
                    "role": "master",
                }
            ],
        )
        monkeypatch.setattr(environment, "_default_code_search_roots", lambda: [])

        result = environment.inspect_running_platforms({})

    assert "platform=v4e2e" in result
    assert "platform=klonet_workflow_e2e" not in result
    assert "code_only_count=0" in result


def test_process_rows_inherit_cross_user_cwd_only_through_observed_parent_chain(
    monkeypatch,
):
    from types import SimpleNamespace

    from klonet_agent.tools import environment

    output = "\n".join([
        "pid=100 pgid=100 ppid=1 uid=997 exe=SCREEN cwd=? "
        "cmd=SCREEN -dmS 102_m bash -lc cd /srv/102/mains && exec python -m "
        "gunicorn -c gun.py master_main:flask_app",
        "pid=101 pgid=101 ppid=100 uid=997 exe=/usr/bin/python3.8 cwd=? "
        "cmd=/usr/bin/python3.8 -m gunicorn -c gun.py master_main:flask_app",
        "pid=102 pgid=101 ppid=101 uid=997 exe=/usr/bin/python3.8 cwd=? "
        "cmd=/usr/bin/python3.8 -m gunicorn -c gun.py master_main:flask_app",
    ])
    monkeypatch.setattr(environment, "_probe_command", lambda name: ["probe"])
    monkeypatch.setattr(
        environment.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=output, stderr="",
        ),
    )
    monkeypatch.setattr(environment, "_privileged_process_cwd", lambda pid: "?")

    rows = environment._process_instance_rows()
    by_pid = {row["pid"]: row for row in rows}

    assert by_pid[101]["cwd"] == "/srv/102/mains"
    assert by_pid[102]["cwd"] == "/srv/102/mains"
    assert by_pid[101]["platform"] == "102"


def test_running_platform_inspection_enumerates_default_code_only_roots(monkeypatch):
    from klonet_agent.tools import environment
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        running_root = temp_dir / "running"
        running_mains = running_root / "mains"
        code_only_root = temp_dir / "code-only"
        code_only_mains = code_only_root / "mains"
        snapshot_mains = temp_dir / "knowledge" / "klonet_source" / "mains"
        for mains in (running_mains, code_only_mains, snapshot_mains):
            mains.mkdir(parents=True)
            (mains / "master_main.py").write_text("# master\n", encoding="utf-8")
            (mains / "worker_main.py").write_text("# worker\n", encoding="utf-8")
        monkeypatch.setattr(
            environment,
            "_process_instance_rows",
            lambda: [
                {
                    "pid": 10,
                    "cwd": str(running_root),
                    "cmd": "gunicorn master_main:flask_app",
                    "platform": "running",
                    "role": "master",
                }
            ],
        )
        monkeypatch.setattr(
            environment, "_default_code_search_roots", lambda: [temp_dir]
        )

        result = environment.inspect_running_platforms({})

    assert "code_only_count=1" in result
    assert "code_only_root=%s" % code_only_root in result
    assert "code_only_root=%s" % running_root not in result
    assert "klonet_source" not in result


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


def test_ops_file_reader_redacts_source_secret_assignments():
    from klonet_agent.tools.environment import read_ops_file
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        config = temp_dir / "app_factory.py"
        config.write_text(
            "\n".join(
                [
                    "master_port = 12000",
                    "flask_app.config['SECRET_KEY'] = b'raw-secret-bytes'",
                    'settings = {"api_token": "json-token-value"}',
                    'headers = {"Authorization": "Bearer bearer-token-value"}',
                    "client_secret: yaml-secret-value",
                ]
            ),
            encoding="utf-8",
        )

        result = read_ops_file({"path": str(config), "max_chars": 1000})

    assert "master_port = 12000" in result
    assert "raw-secret-bytes" not in result
    assert "json-token-value" not in result
    assert "bearer-token-value" not in result
    assert "yaml-secret-value" not in result
    assert result.count("[REDACTED]") >= 4


def test_ops_file_reader_rejects_env_files():
    from klonet_agent.tools.environment import read_ops_file
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        env_file = temp_dir / ".env"
        env_file.write_text("OPENAI_API_KEY=secret", encoding="utf-8")

        result = read_ops_file({"path": str(env_file)})

    assert result.startswith("Error:")
    assert "refused" in result.lower()


def test_root_file_reader_uses_helper_without_sensitive_name_filter(monkeypatch):
    from klonet_agent.tools.environment import read_root_file

    def fake_run(command, **kwargs):
        assert command[:4] == ["sudo", "-n", "/usr/local/bin/klonet-agent-op", "read-file"]
        assert "--path" in command
        assert "/root/.ssh/id_rsa" in command
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "klonet_agent_op\n"
                "action=read-file\n"
                "dry_run=false\n"
                "path=/root/.ssh/id_rsa\n"
                "environment_changed=false\n"
                "content:\n"
                "PRIVATE KEY TEST\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("klonet_agent.tools.environment.subprocess.run", fake_run)

    result = read_root_file({"path": "/root/.ssh/id_rsa", "max_chars": 200})

    assert "read_root_file" in result
    assert "environment_changed=false" in result
    assert "PRIVATE KEY TEST" in result


def test_ops_file_reader_falls_back_to_root_helper_on_permission_error(monkeypatch):
    from klonet_agent.tools.environment import read_ops_file

    def fake_run(command, **kwargs):
        assert command[:4] == ["sudo", "-n", "/usr/local/bin/klonet-agent-op", "read-file"]
        assert "/root/vemu_install_new_gen/base_requ_setup.sh" in command
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "klonet_agent_op\n"
                "action=read-file\n"
                "dry_run=false\n"
                "path=/root/vemu_install_new_gen/base_requ_setup.sh\n"
                "environment_changed=false\n"
                "content:\n"
                "#!/bin/bash\n"
                "install python\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("klonet_agent.tools.environment.subprocess.run", fake_run)

    result = read_ops_file({"path": "/root/vemu_install_new_gen/base_requ_setup.sh"})

    assert "read_ops_file" in result
    assert "environment_changed=false" in result
    assert "install python" in result


def test_install_script_inspection_falls_back_to_root_helper(monkeypatch):
    from klonet_agent.tools.environment import inspect_install_scripts

    def fake_run(command, **kwargs):
        assert command[:4] == ["sudo", "-n", "/usr/local/bin/klonet-agent-op", "inspect-install-scripts"]
        assert "/root/vemu_install_new_gen" in command
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "klonet_agent_op\n"
                "action=inspect-install-scripts\n"
                "dry_run=false\n"
                "script_dir=/root/vemu_install_new_gen\n"
                "environment_changed=false\n"
                "- script=base_requ_setup.sh status=detected executable=false shebang=#!/bin/bash recommended_recipe=run_install_script allowed_args=NORMAL risk_markers=apt-get\n"
                "preflight_status=ready\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("klonet_agent.tools.environment.subprocess.run", fake_run)

    result = inspect_install_scripts({"script_dir": "/root/vemu_install_new_gen"})

    assert "inspect_install_scripts" in result
    assert "root_helper=true" in result
    assert "script=base_requ_setup.sh status=detected" in result
    assert "preflight_status=ready" in result


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
                stdout="1467095 1467011 1467011 1467011 root python3.8 web_terminal_main.py\n",
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


def test_port_owner_keeps_successful_no_listener_result_without_sudo_downgrade(
    monkeypatch,
):
    from types import SimpleNamespace

    from klonet_agent.tools import environment

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["ss", "-ltnp"]:
            return SimpleNamespace(
                returncode=0,
                stdout="State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=1, stdout="", stderr="sudo: a password is required",
        )

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    monkeypatch.setattr(environment.shutil, "which", lambda name: f"/usr/bin/{name}")

    result = environment._port_owner_result(46551)

    assert result.status == "missing"
    assert result.detail == "port=46551 not listening"
    assert calls == [["ss", "-ltnp", "sport = :46551"]]


def test_port_owner_uses_sudo_only_to_enrich_listener_without_visible_pid(
    monkeypatch,
):
    from types import SimpleNamespace

    from klonet_agent.tools import environment

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["ss", "-ltnp"]:
            return SimpleNamespace(
                returncode=0,
                stdout="LISTEN 0 128 0.0.0.0:46552 0.0.0.0:*",
                stderr="",
            )
        return SimpleNamespace(
            returncode=1, stdout="", stderr="sudo: a password is required",
        )

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    monkeypatch.setattr(environment.shutil, "which", lambda name: f"/usr/bin/{name}")

    result = environment._port_owner_result(46552)

    assert result.status == "detected"
    assert "pid=unchecked" in result.detail
    assert calls[1][:3] == ["sudo", "-n", "/usr/bin/ss"]


def test_runtime_port_owner_prefers_process_tree_root_for_shared_listener(monkeypatch):
    from types import SimpleNamespace

    from klonet_agent.tools import environment

    def fake_run(command, **kwargs):
        if command[:2] == ["ss", "-ltnp"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    'LISTEN 0 2048 0.0.0.0:45551 0.0.0.0:* '
                    'users:(("python3.8",pid=2444773,fd=14),'
                    '("python3.8",pid=2148367,fd=14),'
                    '("python3.8",pid=2148261,fd=14))'
                ),
                stderr="",
            )
        if command[:2] == ["ps", "-p"]:
            pid = command[2]
            rows = {
                "2444773": "2444773 2148261 2148261 2148261 lzl python3.8 gun.py master_main:flask_app\n",
                "2148367": "2148367 2148261 2148261 2148261 lzl python3.8 gun.py master_main:flask_app\n",
                "2148261": "2148261 1 2148261 2148261 lzl python3.8 gun.py master_main:flask_app\n",
            }
            return SimpleNamespace(returncode=0, stdout=rows[pid], stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    monkeypatch.setattr(environment.os, "name", "posix")
    monkeypatch.setattr(environment.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        environment,
        "_read_proc_text",
        lambda path: "python3.8 gun.py master_main:flask_app"
        if path.endswith("/cmdline")
        else "",
    )
    monkeypatch.setattr(environment, "_read_proc_link", lambda path: "/home/lzl/test/vemu_uestc")

    result = environment.inspect_process_detail({"ports": [45551]})

    assert "port_owner: detected" in result
    assert "port=45551" in result
    assert "pid=2148261" in result
    assert "listener_pids=2444773,2148367,2148261" in result
    assert "tree_root_pid=2148261" in result
    assert "ppid=1" in result
    assert "pgid=2148261" in result
    assert "sid=2148261" in result


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
                stdout="1467095 1467011 1467011 1467011 root python3.8 web_terminal_main.py\n",
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


def test_process_detail_reports_unchecked_when_proc_cwd_is_unreadable(monkeypatch):
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
                stdout="1467095 1467011 1467011 1467011 root python3.8 web_terminal_main.py\n",
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
    monkeypatch.setattr(environment.os, "readlink", lambda path: (_ for _ in ()).throw(PermissionError("denied")))

    result = environment.inspect_process_detail({"ports": [5045]})

    assert "pid=1467095" in result
    assert "cwd=unchecked" in result
    assert "/proc/1467095/cwd" not in result


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


def test_system_environment_can_probe_allowlisted_command_paths(monkeypatch):
    from types import SimpleNamespace

    from klonet_agent.tools import environment
    from klonet_agent.tools.registry import TOOLS

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout="/usr/local/bin/gunicorn\ngunicorn (version 20.1.0)\n",
            stderr="",
        )

    monkeypatch.setattr(environment.os, "name", "posix")
    monkeypatch.setattr(environment.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(environment.subprocess, "run", fake_run)

    result = environment.inspect_system_environment(
        {"checks": ["command_paths"], "commands": ["gunicorn", "celery", "bad;rm"]}
    )
    system_tool = next(
        item
        for item in TOOLS
        if item["function"]["name"] == "inspect_system_environment"
    )
    checks = system_tool["function"]["parameters"]["properties"]["checks"]["items"]["enum"]

    assert "command_paths" in checks
    assert "command_paths: detected" in result
    assert "gunicorn" in result
    assert "celery" in result
    assert "bad;rm" not in result
    assert any("command -v gunicorn" in " ".join(call) for call in calls)


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


def test_privilege_capability_probe_reports_verified_channels(monkeypatch):
    from types import SimpleNamespace

    from klonet_agent.tools import environment

    monkeypatch.setattr(environment.shutil, "which", lambda name: "/usr/bin/sudo")
    monkeypatch.setattr(
        environment.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    output = environment.inspect_privilege_capabilities({})

    assert "inspect_privilege_capabilities" in output
    assert "sudo_noninteractive=true" in output
    assert "capability_policy=direct_then_controlled_privilege" in output
    assert "environment unchanged" in output


def test_runtime_role_parser_prefers_exact_entrypoint_over_gunicorn_config_substring():
    from klonet_agent.tools.environment import _role_from_command

    assert _role_from_command(
        "python3.8 -m gunicorn -c data_server_gun.py "
        "data_server_main:flask_app"
    ) == "data_server"
    assert _role_from_command(
        "python3.8 -m gunicorn -c gun.py master_main:flask_app"
    ) == "master"
    assert _role_from_command(
        "python3.8 -m gunicorn -c worker_gun.py worker_main:flask_app"
    ) == "worker"


def test_runtime_role_parser_does_not_infer_master_from_unrelated_gun_suffix():
    from klonet_agent.tools.environment import _role_from_command

    assert _role_from_command("python3.8 helper_data_server_gun.py") == ""
