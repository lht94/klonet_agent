"""Policy for structured Ops command execution.

The model may request an Ops command with program/argv/cwd. This module decides
whether that command is allowed, whether it needs sudo, and whether the step
must be confirmed explicitly.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import re
import shutil
from pathlib import Path
from typing import Mapping


MAX_ARGV = 40
MAX_ARG_LEN = 500
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:+/@=-]{1,200}$")
SAFE_GIT_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./@+-]{0,200}$")
SAFE_GIT_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
SAFE_GIT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./@+:-]{0,200}$")
SAFE_GIT_URL = re.compile(
    r"^(?:https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+|(?:git@)?[A-Za-z0-9_.-]+:[A-Za-z0-9_./-]+\.git)$"
)
SAFE_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:-]{0,120}$")
SAFE_MODULE = re.compile(r"^[A-Za-z0-9_][-A-Za-z0-9_]{0,120}$")
SYSTEM_INSTALL_DIRS = (
    Path("/usr/lib"),
    Path("/usr/local/lib"),
    Path("/lib/modules"),
)


@dataclass(frozen=True)
class OpsCommandDecision:
    allowed: bool
    reason: str = ""
    program: str = ""
    argv: tuple[str, ...] = ()
    cwd: str = ""
    risk: str = "normal"
    requires_sudo: bool = False
    requires_step_confirmation: bool = False
    category: str = ""

    def argv_json(self) -> str:
        return json.dumps(list(self.argv), ensure_ascii=False)


def decide_ops_command(args: Mapping | None) -> OpsCommandDecision:
    values = args if isinstance(args, Mapping) else {}
    program = _basename(str(values.get("program") or "").strip())
    argv = _normalize_argv(values.get("argv", []))
    cwd = str(values.get("cwd") or "").strip()
    if not program:
        return _deny("program_required")
    if not isinstance(argv, list):
        return _deny("argv_must_be_array")
    argv = tuple(str(item) for item in argv)
    if len(argv) > MAX_ARGV:
        return _deny("argv_too_long")
    if any("\x00" in item or "\n" in item or "\r" in item or len(item) > MAX_ARG_LEN for item in argv):
        return _deny("argv_contains_invalid_value")
    if not _valid_cwd(cwd):
        return _deny("invalid_cwd")
    if program in {"bash", "sh", "zsh", "fish", "sudo", "su"}:
        return _deny(f"program_not_allowed={program}")

    if program == "make":
        return _decide_make(program, argv, cwd)
    if program == "git":
        return _decide_git(program, argv, cwd)
    if program in {"apt", "apt-get"}:
        return _decide_apt(program, argv, cwd)
    if program in {"cp", "install"}:
        return _decide_file_install(program, argv, cwd)
    if program == "insmod":
        return _decide_insmod(program, argv, cwd)
    if program == "rmmod":
        return _decide_rmmod(program, argv, cwd)
    if program == "tc":
        return _decide_tc(program, argv, cwd)
    return _deny(f"program_not_allowlisted={program}")


def _decide_make(program: str, argv: tuple[str, ...], cwd: str) -> OpsCommandDecision:
    if not cwd:
        return _deny("make_requires_cwd")
    if any(not _safe_arg(item) for item in argv):
        return _deny("make_arg_not_allowed")
    return _allow(program, argv, cwd, risk="controlled", category="workspace_build")


def _decide_git(program: str, argv: tuple[str, ...], cwd: str) -> OpsCommandDecision:
    readonly = argv[:1] in {("status",), ("rev-parse",)}
    submodule_update = argv[:3] == ("submodule", "update", "--init") and set(argv[3:]) <= {"--recursive"}
    clone = _git_clone_allowed(argv, cwd)
    pull = _git_pull_allowed(argv, cwd)
    push = _git_push_allowed(argv, cwd)
    checkout = _git_checkout_allowed(argv, cwd)
    if readonly:
        return _allow(program, argv, cwd, risk="normal", category="git_readonly")
    if submodule_update:
        if not cwd:
            return _deny("git_submodule_requires_cwd")
        return _allow(program, argv, cwd, risk="controlled", category="workspace_prepare")
    if clone:
        return _allow(program, argv, cwd, risk="controlled", category="git_clone")
    if pull:
        return _allow(program, argv, cwd, risk="controlled", category="git_pull")
    if checkout:
        return _allow(program, argv, cwd, risk="controlled", category="git_checkout")
    if push:
        return _allow(program, argv, cwd, risk="dangerous", step=True, category="git_push")
    return _deny("git_args_not_allowed")


def _decide_apt(program: str, argv: tuple[str, ...], cwd: str) -> OpsCommandDecision:
    if argv == ("update",):
        return _allow(program, argv, cwd, risk="dangerous", sudo=True, step=True, category="system_package_index")
    if len(argv) >= 2 and argv[0] == "install":
        rest = argv[1:]
        options = {item for item in rest if item.startswith("-")}
        packages = [item for item in rest if not item.startswith("-")]
        if not packages:
            return _deny("apt_install_requires_packages")
        if not options <= {"-y", "--yes", "--no-install-recommends"}:
            return _deny("apt_install_option_not_allowed")
        if any(not SAFE_PACKAGE.fullmatch(item) for item in packages):
            return _deny("apt_package_not_allowed")
        return _allow(program, argv, cwd, risk="dangerous", sudo=True, step=True, category="system_package_install")
    return _deny("apt_args_not_allowed")


def _decide_file_install(program: str, argv: tuple[str, ...], cwd: str) -> OpsCommandDecision:
    if program == "cp":
        if len(argv) != 2:
            return _deny("cp_requires_source_and_destination")
        source, destination = argv
    else:
        filtered = tuple(item for item in argv if item not in {"-m", "0644", "0755"})
        if len(filtered) != 2:
            return _deny("install_requires_source_and_destination")
        source, destination = filtered
    if not cwd:
        return _deny(f"{program}_requires_cwd")
    if not _source_within_cwd(source, cwd):
        return _deny("source_must_be_within_cwd")
    if not _destination_in_system_install_dir(destination):
        return _deny("destination_not_allowlisted")
    return _allow(program, argv, cwd, risk="privileged", sudo=True, step=True, category="system_file_install")


def _decide_insmod(program: str, argv: tuple[str, ...], cwd: str) -> OpsCommandDecision:
    if len(argv) != 1:
        return _deny("insmod_requires_single_ko")
    if not cwd:
        return _deny("insmod_requires_cwd")
    ko = argv[0]
    if not ko.endswith(".ko") or not _source_within_cwd(ko, cwd):
        return _deny("ko_must_be_under_cwd")
    return _allow(program, argv, cwd, risk="dangerous", sudo=True, step=True, category="kernel_module_load")


def _decide_rmmod(program: str, argv: tuple[str, ...], cwd: str) -> OpsCommandDecision:
    if len(argv) != 1 or not SAFE_MODULE.fullmatch(argv[0]):
        return _deny("rmmod_requires_safe_module_name")
    return _allow(program, argv, cwd, risk="dangerous", sudo=True, step=True, category="kernel_module_unload")


def _decide_tc(program: str, argv: tuple[str, ...], cwd: str) -> OpsCommandDecision:
    if len(argv) >= 2 and argv[:2] == ("qdisc", "show"):
        return _allow(program, argv, cwd, risk="normal", category="network_qdisc_readonly")
    if len(argv) >= 5 and argv[:2] == ("qdisc", "add") and "dev" in argv:
        return _allow(program, argv, cwd, risk="dangerous", sudo=True, step=True, category="network_qdisc_change")
    if len(argv) >= 5 and argv[:2] == ("qdisc", "del") and "dev" in argv:
        return _allow(program, argv, cwd, risk="dangerous", sudo=True, step=True, category="network_qdisc_change")
    return _deny("tc_args_not_allowed")


def _allow(
    program: str,
    argv: tuple[str, ...],
    cwd: str,
    *,
    risk: str,
    category: str,
    sudo: bool = False,
    step: bool = False,
) -> OpsCommandDecision:
    return OpsCommandDecision(
        True,
        program=program,
        argv=argv,
        cwd=cwd,
        risk=risk,
        requires_sudo=sudo,
        requires_step_confirmation=step,
        category=category,
    )


def _deny(reason: str) -> OpsCommandDecision:
    return OpsCommandDecision(False, reason=reason)


def _basename(program: str) -> str:
    return Path(program).name


def _valid_cwd(cwd: str) -> bool:
    if not cwd:
        return True
    return Path(cwd).expanduser().is_absolute() and not any(char in cwd for char in "\x00\n\r")


def _safe_arg(value: str) -> bool:
    return bool(value and SAFE_NAME.fullmatch(value)) or value == ""


def _normalize_argv(raw) -> object:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                return raw
        return parsed if isinstance(parsed, list) else raw
    return raw


def _git_clone_allowed(argv: tuple[str, ...], cwd: str) -> bool:
    if len(argv) != 3 or argv[0] != "clone" or not cwd:
        return False
    repo_url, destination = argv[1], argv[2]
    return bool(SAFE_GIT_URL.fullmatch(repo_url)) and _destination_within_cwd(destination, cwd)


def _git_pull_allowed(argv: tuple[str, ...], cwd: str) -> bool:
    if not cwd or not argv or argv[0] != "pull":
        return False
    if len(argv) == 1:
        return True
    if len(argv) == 3:
        return bool(SAFE_GIT_REMOTE.fullmatch(argv[1]) and SAFE_GIT_REF.fullmatch(argv[2]))
    return False


def _git_push_allowed(argv: tuple[str, ...], cwd: str) -> bool:
    if not cwd or not argv or argv[0] != "push":
        return False
    if len(argv) == 1:
        return True
    if len(argv) == 3:
        return bool(SAFE_GIT_REMOTE.fullmatch(argv[1]) and SAFE_GIT_REF.fullmatch(argv[2]))
    if len(argv) == 4 and argv[1] == "-u":
        return bool(SAFE_GIT_REMOTE.fullmatch(argv[2]) and SAFE_GIT_REF.fullmatch(argv[3]))
    return False


def _git_checkout_allowed(argv: tuple[str, ...], cwd: str) -> bool:
    if not cwd or not argv or argv[0] not in {"checkout", "switch"}:
        return False
    if len(argv) == 2:
        return bool(SAFE_GIT_BRANCH.fullmatch(argv[1]) and not argv[1].startswith("-"))
    if len(argv) == 3 and argv[1] in {"-b", "-c"}:
        return bool(SAFE_GIT_BRANCH.fullmatch(argv[2]))
    return False


def _source_within_cwd(source: str, cwd: str) -> bool:
    if any(char in source for char in "\x00\n\r"):
        return False
    root = Path(cwd).expanduser().resolve(strict=False)
    candidate = Path(source).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError:
        return False
    return True


def _destination_within_cwd(destination: str, cwd: str) -> bool:
    if any(char in destination for char in "\x00\n\r"):
        return False
    root = Path(cwd).expanduser().resolve(strict=False)
    candidate = Path(destination).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError:
        return False
    return True


def _destination_in_system_install_dir(destination: str) -> bool:
    if any(char in destination for char in "\x00\n\r"):
        return False
    path = Path(destination).expanduser()
    if not path.is_absolute():
        return False
    resolved = path.resolve(strict=False)
    return any(_is_relative_to(resolved, root) for root in SYSTEM_INSTALL_DIRS)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def command_exists(program: str) -> bool:
    return shutil.which(program) is not None
