"""Typed, redacted environment facts used by privileged planning.

The model deliberately separates a source repository from a runnable platform
root.  Secrets are represented only by metadata; their values never enter a
plan prompt or persisted grounding record.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


REQUIRED_ENTRY_FILES = (
    "gun.py",
    "master_main.py",
    "celery_worker.py",
    "web_terminal_main.py",
    "worker_gun.py",
    "worker_main.py",
)


@dataclass(frozen=True)
class SecretFact:
    name: str
    configured: bool
    source: str = ""
    access: str = "reference_only"


@dataclass(frozen=True)
class ProjectLayoutFact:
    candidate_root: str
    source_repo_root: str
    platform_root: str
    backend_package_root: str
    entry_source_root: str
    runtime_cwd: str
    config_path: str
    layout_kind: str
    readiness: str
    violations: tuple[str, ...] = ()


@dataclass(frozen=True)
class NginxFacts:
    binary: str = ""
    config_directories: tuple[str, ...] = ()
    config_paths: tuple[str, ...] = ()
    enabled_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RedisFacts:
    binary: str = ""
    config_paths: tuple[str, ...] = ()
    ports: tuple[int, ...] = ()
    bind_addresses: tuple[str, ...] = ()
    authentication: SecretFact = field(
        default_factory=lambda: SecretFact("redis_password", False)
    )


@dataclass(frozen=True)
class PythonRuntimeFacts:
    executables: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceEndpointFacts:
    name: str
    config_paths: tuple[str, ...] = ()
    ports: tuple[int, ...] = ()
    authentication: SecretFact = field(
        default_factory=lambda: SecretFact("service_credentials", False)
    )


@dataclass(frozen=True)
class HostCapabilityFacts:
    docker_binary: str = ""
    docker_daemon_config: str = ""
    libvirt_binary: str = ""
    ovs_binary: str = ""
    screen_binary: str = ""
    kvm_device_present: bool = False


@dataclass(frozen=True)
class UnifiedEnvironmentFacts:
    schema_version: int = 1
    projects: tuple[ProjectLayoutFact, ...] = ()
    nginx: NginxFacts = field(default_factory=NginxFacts)
    redis: RedisFacts = field(default_factory=RedisFacts)
    mysql: ServiceEndpointFacts = field(
        default_factory=lambda: ServiceEndpointFacts("mysql")
    )
    rabbitmq: ServiceEndpointFacts = field(
        default_factory=lambda: ServiceEndpointFacts("rabbitmq")
    )
    python: PythonRuntimeFacts = field(default_factory=PythonRuntimeFacts)
    capabilities: HostCapabilityFacts = field(
        default_factory=HostCapabilityFacts
    )
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render_for_planner(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]


class EnvironmentFactCollector:
    """Collect bounded read-only facts without returning secret values."""

    def collect(self, candidate_roots: list[str]) -> UnifiedEnvironmentFacts:
        projects = tuple(
            fact
            for raw in candidate_roots
            if (fact := self._project_layout(Path(raw).expanduser())) is not None
        )
        return UnifiedEnvironmentFacts(
            projects=projects,
            nginx=self._nginx_facts(),
            redis=self._redis_facts(projects),
            mysql=self._service_endpoint_facts(
                "mysql",
                projects,
                (
                    Path("/etc/mysql/mysql.conf.d/mysqld.cnf"),
                    Path("/etc/mysql/my.cnf"),
                ),
                (
                    r"(?im)^\s*mysql_port\s*=\s*['\"]?(\d{1,5})",
                    r"(?im)^\s*port\s*=\s*(\d{1,5})",
                ),
                r"(?im)^\s*(?:mysql_(?:password|user)|password)\s*=",
            ),
            rabbitmq=self._service_endpoint_facts(
                "rabbitmq",
                projects,
                (
                    Path("/etc/rabbitmq/rabbitmq.conf"),
                    Path("/etc/rabbitmq/rabbitmq-env.conf"),
                ),
                (
                    r"(?im)^\s*rabbitmq_port\s*=\s*['\"]?(\d{1,5})",
                    r"(?im)^\s*listeners\.tcp\.default\s*=\s*(\d{1,5})",
                ),
                r"(?im)^\s*(?:rabbitmq_(?:password|user)|default_(?:pass|user))\s*=",
            ),
            python=self._python_facts(),
            capabilities=self._host_capabilities(),
            warnings=tuple(
                "candidate_is_source_repo_not_runtime_root:%s" % item.candidate_root
                for item in projects
                if item.candidate_root == item.source_repo_root
                and item.platform_root != item.source_repo_root
            ),
        )

    @staticmethod
    def _project_layout(candidate: Path) -> ProjectLayoutFact | None:
        try:
            candidate = candidate.resolve()
        except OSError:
            return None
        if not candidate.is_dir():
            return None

        nested_package = candidate / "vemu_uestc"
        candidate_is_package = (
            (candidate / "__init__.py").is_file()
            and (candidate / "vemu_config").is_dir()
        )
        nested_is_package = (
            (nested_package / "__init__.py").is_file()
            and (nested_package / "vemu_config").is_dir()
        )

        if nested_is_package:
            platform_root = candidate
            backend_root = nested_package
            source_repo_root = nested_package
            layout_kind = "platform_with_nested_backend"
        elif candidate_is_package:
            platform_root = candidate.parent
            backend_root = candidate
            source_repo_root = candidate
            layout_kind = "backend_source_repo"
        else:
            platform_root = candidate
            backend_root = nested_package
            source_repo_root = candidate
            layout_kind = "unrecognized"

        source_candidates = (
            platform_root / "mains",
            backend_root / "mains",
        )
        entry_source = next(
            (
                path
                for path in source_candidates
                if all((path / name).is_file() for name in REQUIRED_ENTRY_FILES)
            ),
            source_candidates[0],
        )
        config_path = backend_root / "vemu_config" / "config.py"
        violations = []
        if not (backend_root / "__init__.py").is_file():
            violations.append("backend_package_missing")
        if not config_path.is_file():
            violations.append("backend_config_missing")
        if not all((entry_source / name).is_file() for name in REQUIRED_ENTRY_FILES):
            violations.append("entry_sources_missing")

        runtime_ready = all(
            (platform_root / name).is_file() for name in REQUIRED_ENTRY_FILES
        )
        if violations:
            readiness = "invalid"
        elif runtime_ready:
            readiness = "runnable"
        else:
            readiness = "preparable"

        return ProjectLayoutFact(
            candidate_root=str(candidate),
            source_repo_root=str(source_repo_root),
            platform_root=str(platform_root),
            backend_package_root=str(backend_root),
            entry_source_root=str(entry_source),
            runtime_cwd=str(platform_root),
            config_path=str(config_path),
            layout_kind=layout_kind,
            readiness=readiness,
            violations=tuple(violations),
        )

    @staticmethod
    def _nginx_facts() -> NginxFacts:
        candidates = (
            Path("/etc/nginx/nginx.conf"),
            Path("/etc/nginx/sites-available/default"),
        )
        enabled_dir = Path("/etc/nginx/sites-enabled")
        enabled = ()
        try:
            if enabled_dir.is_dir():
                enabled = tuple(
                    str(path.resolve())
                    for path in sorted(enabled_dir.iterdir())
                    if path.is_file() or path.is_symlink()
                )[:40]
        except OSError:
            enabled = ()
        return NginxFacts(
            binary=shutil.which("nginx") or "",
            config_directories=tuple(
                str(path)
                for path in (
                    Path("/etc/nginx"),
                    Path("/etc/nginx/sites-available"),
                    Path("/etc/nginx/sites-enabled"),
                    Path("/etc/nginx/conf.d"),
                )
                if path.is_dir()
            ),
            config_paths=tuple(str(path) for path in candidates if path.is_file()),
            enabled_paths=enabled,
        )

    @staticmethod
    def _redis_facts(
        projects: tuple[ProjectLayoutFact, ...],
    ) -> RedisFacts:
        candidates = [
            Path("/etc/redis/redis.conf"),
            Path("/etc/redis.conf"),
        ]
        candidates.extend(Path(item.config_path) for item in projects)
        paths = []
        ports = []
        binds = []
        password_configured = False
        password_source = ""
        for path in candidates:
            if not path.is_file() or str(path) in paths:
                continue
            paths.append(str(path))
            try:
                text = path.read_text(
                    encoding="utf-8-sig",
                    errors="replace",
                )[:500_000]
            except OSError:
                continue
            for match in re.finditer(
                r"(?im)^\s*(?:redis_)?port\s*[=: ]\s*['\"]?(\d{1,5})",
                text,
            ):
                port = int(match.group(1))
                if 1 <= port <= 65535 and port not in ports:
                    ports.append(port)
            for match in re.finditer(
                r"(?im)^\s*bind\s+([^\r\n#]+)",
                text,
            ):
                for address in match.group(1).split():
                    if address not in binds:
                        binds.append(address)
            if re.search(
                r"(?im)^\s*(?:requirepass|masterauth|redis_password)\s*[=: ]\s*(?!['\"]?\s*(?:none|null)?\s*$)\S+",
                text,
            ):
                password_configured = True
                password_source = str(path)
        return RedisFacts(
            binary=shutil.which("redis-server") or "",
            config_paths=tuple(paths),
            ports=tuple(ports),
            bind_addresses=tuple(binds),
            authentication=SecretFact(
                "redis_password",
                password_configured,
                source=password_source,
            ),
        )

    @staticmethod
    def _service_endpoint_facts(
        name: str,
        projects: tuple[ProjectLayoutFact, ...],
        system_candidates: tuple[Path, ...],
        port_patterns: tuple[str, ...],
        authentication_pattern: str,
    ) -> ServiceEndpointFacts:
        candidates = [*system_candidates]
        candidates.extend(Path(item.config_path) for item in projects)
        paths = []
        ports = []
        authentication_configured = False
        authentication_source = ""
        system_paths = {str(path) for path in system_candidates}
        for path in candidates:
            if not path.is_file() or str(path) in paths:
                continue
            paths.append(str(path))
            try:
                text = path.read_text(
                    encoding="utf-8-sig",
                    errors="replace",
                )[:500_000]
            except OSError:
                continue
            for pattern in port_patterns:
                if (
                    name == "mysql"
                    and str(path) not in system_paths
                    and "mysql_port" not in pattern
                ):
                    continue
                for raw in re.findall(pattern, text):
                    port = int(raw)
                    if 1 <= port <= 65535 and port not in ports:
                        ports.append(port)
            if re.search(authentication_pattern, text):
                authentication_configured = True
                authentication_source = str(path)
        return ServiceEndpointFacts(
            name=name,
            config_paths=tuple(paths),
            ports=tuple(ports),
            authentication=SecretFact(
                "%s_credentials" % name,
                authentication_configured,
                source=authentication_source,
            ),
        )

    @staticmethod
    def _host_capabilities() -> HostCapabilityFacts:
        daemon_config = Path("/etc/docker/daemon.json")
        return HostCapabilityFacts(
            docker_binary=shutil.which("docker") or "",
            docker_daemon_config=(
                str(daemon_config) if daemon_config.is_file() else ""
            ),
            libvirt_binary=shutil.which("virsh") or "",
            ovs_binary=shutil.which("ovs-vsctl") or "",
            screen_binary=shutil.which("screen") or "",
            kvm_device_present=Path("/dev/kvm").exists(),
        )

    @staticmethod
    def _python_facts() -> PythonRuntimeFacts:
        candidates = [
            shutil.which("python"),
            shutil.which("python3"),
            "/usr/bin/python3.8",
            "/usr/local/bin/python3.8",
            "/usr/local/python3/bin/python3.8",
        ]
        executables = []
        for raw in candidates:
            if not raw:
                continue
            path = Path(raw)
            if path.is_file():
                resolved = str(path.resolve())
                if resolved not in executables:
                    executables.append(resolved)
        return PythonRuntimeFacts(executables=tuple(executables))
