"""Registered, read-only probes for privileged planning and recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from klonet_agent.ops.privileged.environment_facts import EnvironmentFactCollector
from klonet_agent.tools.environment import (
    inspect_install_scripts,
    inspect_klonet_runtime,
    inspect_nginx_routes,
    inspect_platform_health,
    inspect_platform_instances,
    inspect_process_detail,
    inspect_screen_session,
    inspect_service_health,
    inspect_system_environment,
    read_klonet_logs,
    read_ops_file,
    redact_sensitive_text,
)


@dataclass(frozen=True)
class ReadOnlyProbeSpec:
    name: str
    description: str
    handler: Callable[[dict[str, Any]], str]
    arg_fields: tuple[str, ...] = ()
    sensitivity: str = "internal"


class ReadOnlyProbeRegistry:
    def __init__(self, specs: tuple[ReadOnlyProbeSpec, ...]) -> None:
        self._specs = {spec.name: spec for spec in specs}

    def get(self, name: str) -> ReadOnlyProbeSpec | None:
        return self._specs.get(str(name or "").strip())

    def describe(self) -> tuple[ReadOnlyProbeSpec, ...]:
        return tuple(self._specs.values())

    def render(self) -> str:
        return "\n".join(
            "%s: %s args=%s sensitivity=%s"
            % (
                spec.name,
                spec.description,
                ",".join(spec.arg_fields) or "none",
                spec.sensitivity,
            )
            for spec in self.describe()
        )

    def run(self, name: str, args: dict[str, Any]) -> str:
        spec = self.get(name)
        if spec is None:
            return "probe refused: probe_not_registered=%s" % name
        return redact_sensitive_text(spec.handler(dict(args or {})))


def _project_layout(args: dict[str, Any]) -> str:
    roots = _string_list(args.get("project_roots") or args.get("roots"))
    facts = EnvironmentFactCollector().collect(roots)
    return facts.render_for_planner()


def _python_runtime(args: dict[str, Any]) -> str:
    result = inspect_system_environment(
        {
            "checks": ["python", "system_python", "command_paths"],
            "commands": ["python", "python3", "python3.8", "gunicorn", "celery"],
        }
    )
    return result


def _ports(args: dict[str, Any]) -> str:
    requested = _int_list(args.get("ports"), maximum=128)
    result = _run(["ss", "-ltnp"], timeout=8)
    lines = result.splitlines()
    if requested:
        lines = [
            line for line in lines
            if any(re.search(rf":{port}\b", line) for port in requested)
        ]
    return "inspect_ports\n" + ("\n".join(lines[:300]) or "no matching listeners")


def _process(args: dict[str, Any]) -> str:
    pids = _int_list(args.get("pids"), maximum=64, upper=4_194_304)
    keywords = [
        item for item in _string_list(args.get("keywords"))[:20]
        if re.fullmatch(r"[A-Za-z0-9_.:@/+ -]{1,120}", item)
    ]
    output = _run(
        ["ps", "-eo", "pid,ppid,user,stat,lstart,args", "--sort=pid"],
        timeout=8,
    )
    lines = output.splitlines()
    if pids or keywords:
        selected = lines[:1]
        for line in lines[1:]:
            pid_match = re.match(r"\s*(\d+)", line)
            pid = int(pid_match.group(1)) if pid_match else -1
            if pid in pids or any(word.lower() in line.lower() for word in keywords):
                selected.append(line)
        lines = selected
    details = []
    for pid in pids:
        cwd = _safe_readlink(Path("/proc") / str(pid) / "cwd")
        cmdline = _proc_cmdline(pid)
        details.append("pid=%s cwd=%s cmdline=%s" % (pid, cwd or "unknown", cmdline or "unknown"))
    return "inspect_process\n%s\n%s" % (
        "\n".join(lines[:300]),
        "\n".join(details),
    )


def _process_tree(args: dict[str, Any]) -> str:
    pids = _int_list(args.get("pids"), maximum=32, upper=4_194_304)
    output = _run(
        ["ps", "-eo", "pid,ppid,user,stat,args", "--forest"],
        timeout=8,
    )
    if not pids:
        return "inspect_process_tree\n" + "\n".join(output.splitlines()[:300])
    related = set(pids)
    rows = []
    parsed = []
    for line in output.splitlines()[1:]:
        match = re.match(r"\s*(\d+)\s+(\d+)\s+", line)
        if match:
            parsed.append((int(match.group(1)), int(match.group(2)), line))
    changed = True
    while changed:
        changed = False
        for pid, ppid, _line in parsed:
            if ppid in related and pid not in related:
                related.add(pid)
                changed = True
    for pid, ppid, line in parsed:
        if pid in related or ppid in related:
            rows.append(line)
    return "inspect_process_tree\n" + ("\n".join(rows[:300]) or "no related processes")


def _service(args: dict[str, Any]) -> str:
    services = _safe_names(args.get("services"), maximum=40)
    if not services:
        return inspect_service_health({})
    sections = []
    for service in services:
        active = _run(["systemctl", "is-active", service], timeout=8)
        enabled = _run(["systemctl", "is-enabled", service], timeout=8)
        sections.append(
            "service=%s active=%s enabled=%s"
            % (service, _one_line(active), _one_line(enabled))
        )
    return "inspect_service\n" + "\n".join(sections)


def _screen(args: dict[str, Any]) -> str:
    session = str(args.get("session") or "").strip()
    if session:
        return inspect_screen_session({"session": session})
    return "inspect_screen\n" + _run(["screen", "-ls"], timeout=8)


def _docker(args: dict[str, Any]) -> str:
    name = str(args.get("name") or "").strip()
    argv = ["docker", "ps", "-a", "--no-trunc"]
    if name and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
        argv.extend(["--filter", "name=^/%s$" % name])
    return "inspect_docker\n" + _run(argv, timeout=10)


def _service_subset(name: str) -> Callable[[dict[str, Any]], str]:
    def handler(_args: dict[str, Any]) -> str:
        return inspect_service_health({"services": [name]})

    return handler


def _network(_args: dict[str, Any]) -> str:
    return "inspect_network\n## addresses\n%s\n## routes\n%s" % (
        _run(["ip", "-brief", "address"], timeout=8),
        _run(["ip", "route"], timeout=8),
    )


def _firewall(_args: dict[str, Any]) -> str:
    if shutil.which("ufw"):
        return "inspect_firewall\n" + _run(["ufw", "status", "verbose"], timeout=8)
    if shutil.which("nft"):
        return "inspect_firewall\n" + _run(["nft", "list", "ruleset"], timeout=8)
    if shutil.which("iptables"):
        return "inspect_firewall\n" + _run(["iptables", "-S"], timeout=8)
    return "inspect_firewall\nno supported firewall command found"


def _disk(_args: dict[str, Any]) -> str:
    return "inspect_disk\n" + _run(["df", "-h"], timeout=8)


def _memory(_args: dict[str, Any]) -> str:
    return "inspect_memory\n" + _run(["free", "-h"], timeout=8)


def _virtualization(_args: dict[str, Any]) -> str:
    kvm = Path("/dev/kvm")
    return (
        "inspect_virtualization\n"
        "kvm_device_exists=%s kvm_readable=%s kvm_writable=%s\n"
        "virtualization=%s\ncpu=%s"
    ) % (
        str(kvm.exists()).lower(),
        str(kvm.exists() and os.access(str(kvm), os.R_OK)).lower(),
        str(kvm.exists() and os.access(str(kvm), os.W_OK)).lower(),
        _run(["systemd-detect-virt"], timeout=8),
        _run(["lscpu"], timeout=8),
    )


def _libvirt(_args: dict[str, Any]) -> str:
    return "inspect_libvirt\n## domains\n%s\n## networks\n%s\n## node\n%s" % (
        _run(["virsh", "list", "--all"], timeout=10),
        _run(["virsh", "net-list", "--all"], timeout=10),
        _run(["virsh", "nodeinfo"], timeout=10),
    )


def _ovs(_args: dict[str, Any]) -> str:
    return "inspect_ovs\n## overview\n%s\n## bridges\n%s" % (
        _run(["ovs-vsctl", "show"], timeout=10),
        _run(["ovs-vsctl", "list-br"], timeout=10),
    )


def _docker_networks(_args: dict[str, Any]) -> str:
    return "inspect_docker_networks\n" + _run(
        ["docker", "network", "ls", "--no-trunc"],
        timeout=10,
    )


def _docker_images(_args: dict[str, Any]) -> str:
    return "inspect_docker_images\n" + _run(
        ["docker", "images", "--digests", "--no-trunc"],
        timeout=10,
    )


def _network_links(args: dict[str, Any]) -> str:
    names = _safe_names(args.get("names"), maximum=40)
    if names:
        sections = [
            _run(["ip", "-details", "link", "show", "dev", name], timeout=8)
            for name in names
        ]
        return "inspect_network_links\n" + "\n".join(sections)
    return "inspect_network_links\n" + _run(
        ["ip", "-details", "-brief", "link"],
        timeout=8,
    )


def _file_integrity(args: dict[str, Any]) -> str:
    rows = []
    for raw in _string_list(args.get("paths"))[:50]:
        path = _absolute_file(raw)
        if path is None:
            rows.append("path=%s status=missing_or_invalid" % raw)
            continue
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            rows.append(
                "path=%s size=%s sha256=%s"
                % (path, path.stat().st_size, digest.hexdigest())
            )
        except OSError as exc:
            rows.append(
                "path=%s status=unavailable reason=%s"
                % (path, exc.__class__.__name__)
            )
    return "inspect_file_integrity\n" + "\n".join(rows)


def _json_file(args: dict[str, Any]) -> str:
    path = _absolute_file(args.get("path"))
    if path is None or path.suffix.lower() != ".json":
        return "inspect_json_file\ninvalid_or_missing_json_path"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return "inspect_json_file\nvalid=false reason=%s" % exc.__class__.__name__
    if isinstance(value, dict):
        return "inspect_json_file\npath=%s valid=true type=object keys=%s" % (
            path,
            ",".join(sorted(str(key) for key in value)[:100]),
        )
    return "inspect_json_file\npath=%s valid=true type=%s" % (
        path,
        type(value).__name__,
    )


def _archive_inventory(args: dict[str, Any]) -> str:
    path = _absolute_file(args.get("path"))
    if path is None:
        return "inspect_archive\ninvalid_or_missing_archive"
    try:
        if tarfile.is_tarfile(str(path)):
            with tarfile.open(str(path), "r:*") as opened:
                result = "\n".join(
                    "%s type=%s size=%s"
                    % (
                        item.name,
                        (
                            "directory" if item.isdir()
                            else "symlink" if item.issym() or item.islnk()
                            else "device" if item.isdev()
                            else "file"
                        ),
                        item.size,
                    )
                    for item in opened.getmembers()[:300]
                )
        elif zipfile.is_zipfile(str(path)):
            with zipfile.ZipFile(str(path)) as opened:
                result = "\n".join(
                    "%s type=%s size=%s"
                    % (
                        item.filename,
                        "directory" if item.is_dir() else "file",
                        item.file_size,
                    )
                    for item in opened.infolist()[:300]
                )
        else:
            result = "unsupported_archive_type"
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        result = "archive_read_failed=%s" % exc.__class__.__name__
    return "inspect_archive\npath=%s\n%s" % (
        path,
        result,
    )


def _klonet_config_consistency(args: dict[str, Any]) -> str:
    root = _absolute_existing_directory(args.get("project_root"))
    if root is None:
        return "inspect_klonet_config_consistency\ninvalid_project_root"
    candidates = (
        root / "vemu_uestc" / "vemu_config" / "config.py",
        root / "vemu_config" / "config.py",
    )
    config = next((path for path in candidates if path.is_file()), None)
    if config is None:
        return "inspect_klonet_config_consistency\nbackend_config_missing"
    content = config.read_text(encoding="utf-8", errors="replace")
    keys = (
        "master_ip",
        "master_port",
        "worker_port",
        "public_port",
        "web_terminal_port",
        "redis_port",
        "mysql_port",
        "rabbitmq_port",
    )
    rows = []
    for key in keys:
        values = re.findall(
            r"(?m)^\s*%s\s*=\s*([^#\r\n]+)" % re.escape(key),
            content,
        )
        if values:
            rows.append(
                "%s=%s"
                % (key, "|".join(_one_line(item) for item in values[-8:]))
            )
    active = re.findall(
        r"(?m)^\s*PROJ_CONFIG\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        content,
    )
    entry = root / "web_terminal_main.py"
    if not entry.is_file():
        entry = root / "mains" / "web_terminal_main.py"
    terminal_values = []
    if entry.is_file():
        terminal_values = re.findall(
            r"(?m)(?:listen|port)\s*=\s*(\d{2,5})",
            entry.read_text(encoding="utf-8", errors="replace"),
        )
    return (
        "inspect_klonet_config_consistency\nproject_root=%s config=%s "
        "active_config=%s\n%s\nweb_terminal_entry_ports=%s"
    ) % (
        root,
        config,
        active[-1] if active else "unknown",
        "\n".join(rows),
        ",".join(terminal_values) or "not_explicitly_detected",
    )


def _git_repository(args: dict[str, Any]) -> str:
    path = _absolute_existing_directory(args.get("repository") or args.get("path"))
    if path is None:
        return "inspect_git_repository\ninvalid_or_missing_repository"
    inside = _one_line(
        _run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], timeout=10)
    )
    revision = _one_line(
        _run(["git", "-C", str(path), "rev-parse", "HEAD"], timeout=10)
    )
    status = _run(
        ["git", "-C", str(path), "status", "--short", "--branch"],
        timeout=10,
    )
    remotes = _run(["git", "-C", str(path), "remote", "-v"], timeout=10)
    return (
        "inspect_git_repository\n"
        "path=%s inside_work_tree=%s revision=%s\n"
        "status=%s\nremotes=%s"
    ) % (
        path,
        inside or "unknown",
        revision or "unknown",
        status or "clean",
        remotes or "none",
    )


def _tcp_connection(args: dict[str, Any]) -> str:
    host = str(args.get("host") or "127.0.0.1").strip()
    ports = _int_list(args.get("ports"), maximum=64)
    timeout = min(max(float(args.get("timeout") or 2), 0.1), 10)
    if not _safe_host(host) or not ports:
        return "probe_tcp_connection\ninvalid_host_or_ports"
    rows = []
    for port in ports:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                rows.append("host=%s port=%s status=connected" % (host, port))
        except OSError as exc:
            rows.append(
                "host=%s port=%s status=failed reason=%s"
                % (host, port, exc.__class__.__name__)
            )
    return "probe_tcp_connection\n" + "\n".join(rows)


def _http_endpoint(args: dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or not _safe_host(parsed.hostname)
    ):
        return "probe_http_endpoint\ninvalid_or_sensitive_url"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = response.read(1000).decode("utf-8", errors="replace")
            return "probe_http_endpoint\nstatus=%s body=%s" % (
                response.status,
                _one_line(body),
            )
    except (urllib.error.URLError, OSError) as exc:
        return "probe_http_endpoint\nfailed=%s" % exc.__class__.__name__


def _python_import(args: dict[str, Any]) -> str:
    python = _absolute_file(args.get("python_executable"))
    cwd = _absolute_existing_directory(args.get("cwd"))
    raw_modules = args.get("modules")
    if not raw_modules and args.get("module"):
        raw_modules = [args.get("module")]
    modules = _string_list(raw_modules)[:30]
    if (
        python is None
        or cwd is None
        or not modules
        or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,200}", item) for item in modules)
    ):
        return "probe_python_import\ninvalid_python_cwd_or_modules"
    code = (
        "import importlib\n"
        "mods=%s\n"
        "for name in mods:\n"
        " module=importlib.import_module(name)\n"
        " print('module=' + name + ' import_ok=true file=' + str(getattr(module, '__file__', 'built-in')))\n"
    ) % json.dumps(modules)
    return "probe_python_import\n" + _run(
        [str(python), "-c", code],
        cwd=cwd,
        timeout=20,
    )


def _path_permissions(args: dict[str, Any]) -> str:
    paths = _string_list(args.get("paths"))[:50]
    rows = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            rows.append("path=%s status=refused_relative" % raw)
            continue
        try:
            stat = path.stat()
            rows.append(
                "path=%s exists=true mode=%s uid=%s gid=%s"
                % (path.resolve(), oct(stat.st_mode & 0o7777), stat.st_uid, stat.st_gid)
            )
        except OSError as exc:
            rows.append("path=%s exists=false reason=%s" % (path, exc.__class__.__name__))
    return "inspect_path_permissions\n" + "\n".join(rows)


DEFAULT_READONLY_PROBES = ReadOnlyProbeRegistry(
    (
        ReadOnlyProbeSpec("system_environment", "操作系统、Python、磁盘和命令路径", inspect_system_environment),
        ReadOnlyProbeSpec("project_layout", "源码、Python 包、入口和运行根目录关系", _project_layout, ("project_roots",)),
        ReadOnlyProbeSpec("python_runtime", "Python 解释器及 Gunicorn/Celery 路径", _python_runtime),
        ReadOnlyProbeSpec("ports", "监听端口及进程摘要", _ports, ("ports",)),
        ReadOnlyProbeSpec("port_owner", "指定端口的 PID/进程所有者", inspect_process_detail, ("ports",)),
        ReadOnlyProbeSpec("process", "PID、用户、状态、命令和 cwd", _process, ("pids", "keywords")),
        ReadOnlyProbeSpec("process_tree", "指定 PID 及其子进程树", _process_tree, ("pids",)),
        ReadOnlyProbeSpec("service", "指定 systemd 服务状态", _service, ("services",)),
        ReadOnlyProbeSpec("screen", "screen 列表或指定会话输出", _screen, ("session",)),
        ReadOnlyProbeSpec("docker", "Docker 容器状态", _docker, ("name",)),
        ReadOnlyProbeSpec("nginx", "Nginx 路由配置", inspect_nginx_routes, ("paths",)),
        ReadOnlyProbeSpec("redis", "Redis 服务状态，不读取密码值", _service_subset("redis"), sensitivity="secret_metadata"),
        ReadOnlyProbeSpec("mysql", "MySQL 服务状态", _service_subset("mysql")),
        ReadOnlyProbeSpec("rabbitmq", "RabbitMQ 服务状态", _service_subset("rabbitmq")),
        ReadOnlyProbeSpec("network", "接口地址和路由", _network),
        ReadOnlyProbeSpec("firewall", "UFW/nftables/iptables 规则", _firewall),
        ReadOnlyProbeSpec("disk", "文件系统容量", _disk),
        ReadOnlyProbeSpec("memory", "内存和 swap", _memory),
        ReadOnlyProbeSpec("virtualization", "KVM 设备、虚拟化类型和 CPU 能力", _virtualization),
        ReadOnlyProbeSpec("libvirt", "libvirt domain、network 和节点能力", _libvirt),
        ReadOnlyProbeSpec("ovs", "Open vSwitch bridge、port 和控制器状态", _ovs),
        ReadOnlyProbeSpec("docker_networks", "Docker 网络列表和驱动", _docker_networks),
        ReadOnlyProbeSpec("docker_images", "Docker 镜像、tag 和 digest", _docker_images),
        ReadOnlyProbeSpec("network_links", "宿主机 link、tap、veth、bridge 和状态", _network_links, ("names",)),
        ReadOnlyProbeSpec("file_integrity", "明确文件的大小与 SHA-256", _file_integrity, ("paths",)),
        ReadOnlyProbeSpec("json_file", "JSON 配置语法和顶层键，不输出值", _json_file, ("path",)),
        ReadOnlyProbeSpec("archive_inventory", "安装包归档成员清单", _archive_inventory, ("path",)),
        ReadOnlyProbeSpec(
            "klonet_config_consistency",
            "后端活动配置类、关键端口与 Web Terminal 入口一致性",
            _klonet_config_consistency,
            ("project_root",),
        ),
        ReadOnlyProbeSpec("git_repository", "Git 状态、revision 和 remote", _git_repository, ("repository",)),
        ReadOnlyProbeSpec("logs", "脱敏后的日志尾部", read_klonet_logs, ("path",)),
        ReadOnlyProbeSpec("tcp_connection", "指定主机端口 TCP 连通性", _tcp_connection, ("host", "ports")),
        ReadOnlyProbeSpec("http_endpoint", "HTTP(S) 健康端点", _http_endpoint, ("url",)),
        ReadOnlyProbeSpec("python_import", "使用目标解释器和 cwd 验证模块导入", _python_import, ("python_executable", "cwd", "modules")),
        ReadOnlyProbeSpec("path_permissions", "路径 mode、uid 和 gid", _path_permissions, ("paths",)),
        # Compatibility names used by existing recovery plans.
        ReadOnlyProbeSpec("klonet_runtime", "Klonet 综合运行状态", inspect_klonet_runtime),
        ReadOnlyProbeSpec("platform_instances", "发现平台实例", inspect_platform_instances, ("project_roots",)),
        ReadOnlyProbeSpec("platform_health", "指定平台健康状态", inspect_platform_health, ("project_root",)),
        ReadOnlyProbeSpec("service_health", "共享服务健康状态", inspect_service_health),
        ReadOnlyProbeSpec("process_detail", "端口/PID/关键词进程详情", inspect_process_detail),
        ReadOnlyProbeSpec("ops_file", "脱敏读取一个运维文件", read_ops_file, ("path",)),
        ReadOnlyProbeSpec("screen_session", "指定 screen 会话输出", inspect_screen_session, ("session",)),
        ReadOnlyProbeSpec("nginx_routes", "Nginx 路由配置", inspect_nginx_routes, ("paths",)),
        ReadOnlyProbeSpec("install_scripts", "安装脚本存在性与风险标记", inspect_install_scripts, ("script_dir",)),
    )
)


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 8,
) -> str:
    if not argv or not shutil.which(argv[0]):
        return "command_unavailable=%s" % (argv[0] if argv else "missing")
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "command_failed=%s" % exc.__class__.__name__
    output = "%s\n%s" % (completed.stdout or "", completed.stderr or "")
    return redact_sensitive_text(output.strip())[:20000]


def _absolute_existing_directory(value) -> Path | None:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        return None
    try:
        path = path.resolve()
    except OSError:
        return None
    return path if path.is_dir() else None


def _absolute_file(value) -> Path | None:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        return None
    try:
        path = path.resolve()
    except OSError:
        return None
    return path if path.is_file() else None


def _string_list(value) -> list[str]:
    return [str(item).strip() for item in value[:100] if str(item).strip()] if isinstance(value, list) else []


def _int_list(
    value,
    *,
    maximum: int,
    upper: int = 65535,
) -> list[int]:
    result = []
    if not isinstance(value, list):
        return result
    for item in value[:maximum]:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= upper and number not in result:
            result.append(number)
    return result


def _safe_names(value, *, maximum: int) -> list[str]:
    return [
        item for item in _string_list(value)[:maximum]
        if re.fullmatch(r"[A-Za-z0-9_.@:-]{1,160}", item)
    ]


def _safe_host(host: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.:\[\]-]{1,253}", host))


def _safe_readlink(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return ""


def _proc_cmdline(pid: int) -> str:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return " ".join(
        part.decode("utf-8", errors="replace")
        for part in raw.split(b"\x00")
        if part
    )


def _one_line(value, limit: int = 1500) -> str:
    return " ".join(str(value or "").split())[:limit]
