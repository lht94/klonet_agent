from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICE_TEMPLATE = PROJECT_ROOT / "scripts" / "klonet-agent.service.in"


def test_service_template_runs_as_dedicated_account():
    text = SERVICE_TEMPLATE.read_text(encoding="utf-8")

    assert "User=klonet-agent" in text
    assert "Group=klonet-agent" in text
    assert "WorkingDirectory=@PACKAGE_PARENT@" in text
    assert "EnvironmentFile=-@ENV_FILE@" in text
    assert "ExecStart=@PYTHON@ -m klonet_agent.agent --mode @MODE@" in text
    assert "--user-id @USER_ID@ --project-id @PROJECT_ID@" in text
    assert "StandardInput=null" in text
    assert "Restart=on-failure" in text
