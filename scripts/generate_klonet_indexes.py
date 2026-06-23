"""从 Klonet 源码生成可检索的机器索引。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import warnings
from pathlib import Path
from typing import Iterable


SOURCE_SUFFIXES = {
    ".py", ".md", ".rst", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".sh", ".html", ".js", ".ts", ".css", ".puml",
}
EXCLUDED_PARTS = {
    ".git", ".history", "__pycache__", ".pytest_cache", "node_modules",
    "logs", "output", "tmp", "test-results", "static_resources",
}
SENSITIVE_NAME = re.compile(
    r"password|passwd|(^|_)pass$|secret|token|credential|authorization|private_key|"
    r"access_key|email|(^|_)ip$|(^|_)host$|domain",
    re.IGNORECASE,
)
IPV4_VALUE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


DOMAIN_RULES = [
    ("topology", ("topo", "subtopo", "resource_manager")),
    ("link", ("link", "vxlan", "veth", "delay", "throughput")),
    ("traffic", ("traffic", "pkt_gen")),
    ("monitor", ("monitor", "prometheus", "sflow", "health")),
    ("vm", ("kvm", "libvirt", "virtual", "web_terminal", "ssh_connect")),
    ("image_registry", ("image_registry", "image_upload", "image_sync")),
    ("satellite", ("satellite", "satool", "master_evt")),
    ("data", ("redis", "mysql", "data_server", "model")),
    ("auth", ("user_", "permission", "login", "authority")),
    ("runtime", ("app_factory", "main.py", "celery_worker", "config.py")),
]


DOMAIN_MAP = {
    "topology": {
        "paths": [
            "webserver/api/topo/",
            "webserver/tasks/topo/",
            "Function_layer/topo_preprocess.py",
            "Function_layer/topo_partition.py",
            "Function_layer/resource_manager.py",
            "Service_layer/TopoManager.py",
        ],
        "notes": "拓扑校验、切分、资源调度、部署和删除。",
    },
    "link": {
        "paths": [
            "webserver/api/link/",
            "Implement_layer/LinkManager/",
            "Service_layer/LinkManager.py",
        ],
        "notes": "Veth、VXLAN、OVS、tc、时延和链路配置。",
    },
    "traffic": {
        "paths": [
            "webserver/api/traffic/",
            "Service_layer/TrafficManager.py",
            "tools/vemu_api/traffic.py",
        ],
        "notes": "流量定义、下发、状态和实时查询。",
    },
    "monitor": {
        "paths": [
            "webserver/api/monitor/",
            "Function_layer/pro_monitor_query.py",
            "Implement_layer/ExprMonitorManager/",
        ],
        "notes": "节点、平台、链路和实验监控。",
    },
    "vm": {
        "paths": [
            "webserver/api/kvm_image/",
            "webserver/api/ssh_connect/",
            "webserver/web_back/web_terminal_impl.py",
            "Service_layer/NEManager.py",
            "Service_layer/kvm_image_upload.py",
        ],
        "notes": "KVM 节点、镜像、组网、SSH 和 Web Terminal。",
    },
    "image_registry": {
        "paths": [
            "webserver/api/image_registry/",
            "Implement_layer/ImageRegistryManager/",
            "Service_layer/image_registry_upload.py",
        ],
        "notes": "容器镜像、实验镜像和仓库操作。",
    },
    "satellite": {
        "paths": [
            "webserver/api/satellite/",
            "webserver/tasks/satellite/",
            "satellite/",
            "Function_layer/satellite.py",
        ],
        "notes": "卫星拓扑、时变事件和天地一体化实验。",
    },
    "data": {
        "paths": [
            "Service_layer/redisAPI.py",
            "Service_layer/mysql_models.py",
            "Service_layer/mysql_api/",
            "webserver/api/data_server/",
        ],
        "notes": "Redis 运行态、MySQL 元数据和数据服务。",
    },
    "auth": {
        "paths": [
            "webserver/api/user_management/",
            "webserver/api/permissions_management/",
            "Service_layer/authority_manager/",
        ],
        "notes": "用户、登录、权限和角色。",
    },
    "runtime": {
        "paths": [
            "mains/",
            "webserver/app_factory.py",
            "webserver/bootstrap/",
            "vemu_config/config.py",
        ],
        "notes": "应用入口、路由装配、启动和配置。",
    },
}


def _write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in materialized:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(materialized)


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if any(part in EXCLUDED_PARTS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES:
            yield path


def _iter_code_files(root: Path) -> Iterable[Path]:
    """Yield runtime source trees, preferring the importable package over mirrors."""
    package_root = root / "vemu_uestc"
    if not package_root.is_dir():
        yield from _iter_source_files(root)
        return

    for scan_root in (package_root, root / "mains", root / "tools"):
        if scan_root.is_dir():
            yield from _iter_source_files(scan_root)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def infer_domain(path: str) -> str:
    lowered = path.lower()
    for domain, needles in DOMAIN_RULES:
        if any(needle in lowered for needle in needles):
            return domain
    return "other"


def _first_docline(node: ast.AST) -> str:
    doc = ast.get_docstring(node, clean=True) or ""
    return doc.splitlines()[0].strip() if doc else ""


def _parse_python(path: Path) -> ast.Module | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            return ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, UnicodeError):
        return None


def _file_rows(root: Path) -> list[dict]:
    rows = []
    for path in _iter_source_files(root):
        rel = _relative(root, path)
        data = path.read_bytes()
        rows.append(
            {
                "path": rel,
                "type": path.suffix.lower().lstrip(".") or "unknown",
                "domain": infer_domain(rel),
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "last_modified": path.stat().st_mtime_ns,
            }
        )
    return rows


def _symbol_rows(root: Path) -> list[dict]:
    rows = []
    for path in _iter_code_files(root):
        if path.suffix.lower() != ".py":
            continue
        tree = _parse_python(path)
        if tree is None:
            continue
        rel = _relative(root, path)
        module = rel[:-3].replace("/", ".")
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if isinstance(node, ast.ClassDef):
                    kind = "class"
                elif isinstance(node, ast.AsyncFunctionDef):
                    kind = "async_function"
                else:
                    kind = "function"
                rows.append(
                    {
                        "path": rel,
                        "module": module,
                        "symbol": node.name,
                        "kind": kind,
                        "line": node.lineno,
                        "end_line": getattr(node, "end_lineno", node.lineno),
                        "domain": infer_domain(rel),
                        "summary": _first_docline(node),
                    }
                )
    return sorted(rows, key=lambda row: (row["path"], row["line"], row["symbol"]))


def _constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _resolve_relative_import(source_root: Path, file: Path, node: ast.ImportFrom, name: str) -> str:
    if node.level:
        base = file.parent
        for _ in range(max(node.level - 1, 0)):
            base = base.parent
    else:
        base = source_root
    if node.module:
        base = base.joinpath(*node.module.split("."))
    candidate_module = base / f"{name}.py"
    candidate_package = base / name / "__init__.py"
    if candidate_module.exists():
        return _relative(source_root, candidate_module)
    if candidate_package.exists():
        return _relative(source_root, candidate_package)
    module_file = base.with_suffix(".py")
    if module_file.exists():
        return _relative(source_root, module_file)
    return ""


def _import_map(source_root: Path, file: Path, tree: ast.Module) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local_name = alias.asname or alias.name
                mapping[local_name] = _resolve_relative_import(source_root, file, node, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".")[-1]
                candidate = source_root.joinpath(*alias.name.split(".")).with_suffix(".py")
                mapping[local_name] = _relative(source_root, candidate) if candidate.exists() else ""
    return mapping


def _view_parts(node: ast.AST) -> tuple[str, str]:
    if isinstance(node, ast.Attribute):
        owner = node.value.id if isinstance(node.value, ast.Name) else ""
        return owner, node.attr
    if isinstance(node, ast.Name):
        return "", node.id
    return "", ""


def _class_details(source_root: Path, implementation: str, class_name: str) -> tuple[list[str], str]:
    if not implementation:
        return [], ""
    path = source_root / implementation
    tree = _parse_python(path)
    if tree is None:
        return [], ""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            methods = [
                child.name.upper()
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name in {"get", "post", "put", "delete", "patch"}
            ]
            return methods, _first_docline(node)
    return [], ""


def _route_rows(root: Path) -> list[dict]:
    rows = []
    app_factories = [
        path for path in _iter_code_files(root) if path.name == "app_factory.py"
    ]
    for path in sorted(app_factories):
        tree = _parse_python(path)
        if tree is None:
            continue
        imports = _import_map(root, path, tree)
        registered_in = _relative(root, path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name != "register_api" or len(node.args) < 3:
                continue
            route = _constant_string(node.args[2])
            endpoint = _constant_string(node.args[1])
            if not route:
                continue
            owner, class_name = _view_parts(node.args[0])
            implementation = imports.get(owner, "") if owner else ""
            methods, summary = _class_details(root, implementation, class_name)
            if route.startswith("/master/") or route.startswith("/my/"):
                side = "master"
            elif route.startswith("/worker/") or route.startswith("/modification/"):
                side = "worker"
            elif route.startswith("/data-server/"):
                side = "data_server"
            else:
                side = "shared"
            rows.append(
                {
                    "route": route,
                    "methods": methods,
                    "view_class": class_name,
                    "endpoint": endpoint or "",
                    "registered_in": registered_in,
                    "implementation": implementation,
                    "domain": infer_domain(f"{route} {implementation}"),
                    "side": side,
                    "summary": summary,
                }
            )
    unique = {
        (row["registered_in"], row["route"], row["view_class"], row["endpoint"]): row
        for row in rows
    }
    return sorted(unique.values(), key=lambda row: (row["route"], row["view_class"]))


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _decorator_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def _celery_task_rows(root: Path) -> list[dict]:
    rows = []
    for path in _iter_code_files(root):
        if path.suffix.lower() != ".py":
            continue
        tree = _parse_python(path)
        if tree is None:
            continue
        rel = _relative(root, path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                decorator_name = _decorator_name(decorator)
                if not (
                    decorator_name.endswith(".task")
                    or decorator_name in {"task", "shared_task"}
                ):
                    continue
                track_started = False
                explicit_name = ""
                if isinstance(decorator, ast.Call):
                    for keyword in decorator.keywords:
                        if keyword.arg == "track_started" and isinstance(keyword.value, ast.Constant):
                            track_started = bool(keyword.value.value)
                        if keyword.arg == "name":
                            explicit_name = _constant_string(keyword.value) or ""
                rows.append(
                    {
                        "path": rel,
                        "symbol": node.name,
                        "task_name": explicit_name or node.name,
                        "line": node.lineno,
                        "track_started": track_started,
                        "domain": infer_domain(rel),
                        "summary": _first_docline(node),
                    }
                )
    return sorted(rows, key=lambda row: (row["path"], row["line"]))


def _safe_default(name: str, value_node: ast.AST) -> tuple[object, bool, str]:
    try:
        value = ast.literal_eval(value_node)
    except (ValueError, TypeError):
        return "<dynamic>", False, type(value_node).__name__
    value_type = type(value).__name__
    sensitive = bool(SENSITIVE_NAME.search(name))
    if isinstance(value, str) and IPV4_VALUE.fullmatch(value.strip()):
        sensitive = True
    if sensitive:
        return "<redacted>", True, value_type
    if isinstance(value, (str, int, float, bool, type(None))):
        return value, False, value_type
    return f"<{value_type}>", False, value_type


def _config_rows(root: Path) -> list[dict]:
    rows = []
    candidates = [
        path for path in _iter_code_files(root)
        if path.suffix.lower() == ".py"
        and ("config" in path.name.lower() or "settings" in path.name.lower())
    ]
    for path in candidates:
        tree = _parse_python(path)
        if tree is None:
            continue
        rel = _relative(root, path)
        scopes: list[tuple[str, list[ast.stmt]]] = [("<module>", tree.body)]
        scopes.extend((node.name, node.body) for node in tree.body if isinstance(node, ast.ClassDef))
        for scope, statements in scopes:
            for node in statements:
                names: list[str] = []
                value_node: ast.AST | None = None
                if isinstance(node, ast.Assign):
                    names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                    value_node = node.value
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    names = [node.target.id]
                    value_node = node.value
                if value_node is None:
                    continue
                for name in names:
                    default, sensitive, value_type = _safe_default(name, value_node)
                    rows.append(
                        {
                            "path": rel,
                            "scope": scope,
                            "name": name,
                            "default": default,
                            "value_type": value_type,
                            "sensitive": sensitive,
                            "line": node.lineno,
                            "domain": infer_domain(rel),
                        }
                    )
    return sorted(rows, key=lambda row: (row["path"], row["scope"], row["line"], row["name"]))


def _domain_rows(root: Path) -> list[dict]:
    rows = []
    for domain, definition in DOMAIN_MAP.items():
        existing = []
        for path in definition["paths"]:
            relative = Path(path.rstrip("/"))
            package_path = Path("vemu_uestc") / relative
            if (root / package_path).exists():
                suffix = "/" if path.endswith("/") else ""
                existing.append(package_path.as_posix() + suffix)
            elif (root / relative).exists():
                existing.append(path)
        rows.append(
            {
                "domain": domain,
                "paths": existing,
                "notes": definition["notes"],
            }
        )
    return rows


def generate_indexes(source_root: Path, output_root: Path) -> dict[str, int]:
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    datasets = {
        "files": _file_rows(source_root),
        "symbols": _symbol_rows(source_root),
        "routes": _route_rows(source_root),
        "celery_tasks": _celery_task_rows(source_root),
        "config_items": _config_rows(source_root),
        "domain_map": _domain_rows(source_root),
    }
    counts = {}
    for name, rows in datasets.items():
        counts[name] = _write_jsonl(output_root / f"{name}.jsonl", rows)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Klonet source machine indexes.")
    parser.add_argument("--source", default="klonet_knowledge/02_vemu_uestc_code")
    parser.add_argument("--output", default="knowledge/klonet_index")
    args = parser.parse_args()

    counts = generate_indexes(Path(args.source), Path(args.output))
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
