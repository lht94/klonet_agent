"""确定性的命令风险识别与审批策略。"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from klonet_agent.ops.privileged.contracts import PrivilegedStep, RISK_LEVELS


@dataclass(frozen=True)
class RiskDecision:
    risk: str
    auto_authorized: bool = False
    requires_plan_confirmation: bool = False
    requires_step_confirmation: bool = False
    denied: bool = False
    reason: str = ""


class PrivilegedRiskPolicy:
    """以规则结果为下限，防止 Planner 低估命令风险。"""

    _hard_denied = (
        re.compile(r"(^|\s)rm\s+-[^\n]*r[^\n]*f[^\n]*\s+/(?:\s|$)"),
        re.compile(r"\b(?:mkfs(?:\.\w+)?|fdisk|parted)\b"),
        re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"),
        re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sh|bash)\b"),
        re.compile(r"\bsudo\s+(?:-S|--stdin)\b"),
        re.compile(r"\bcurl\b[^\n]*(?:-d|--data(?:-binary)?)\s+@/(?:etc|home)/"),
        re.compile(r"\b(?:scp|rsync)\s+/(?:etc|home)/\S+\s+\S+@"),
        re.compile(r"\bfind\s+/\s+[^\n]*-delete\b"),
    )
    _high = (
        re.compile(r"\brm\s+-[^\n]*r"),
        re.compile(r"\b(?:shutdown|reboot|poweroff)\b"),
        re.compile(r"\b(?:DROP\s+(?:DATABASE|TABLE)|TRUNCATE\s+TABLE)\b", re.I),
        re.compile(r"\bchmod\s+(?:-R\s+)?777\b"),
        re.compile(r"\b(?:userdel|groupdel)\b"),
    )
    _medium = (
        re.compile(r"\b(?:systemctl|service)\s+(?:restart|stop|start|reload)\b"),
        re.compile(r"\b(?:apt|apt-get|dnf|yum|pip|pip3)\s+(?:install|remove|uninstall|upgrade)\b"),
        re.compile(r"\b(?:cp|mv|install|tee|sed\s+-i)\b"),
        re.compile(r"\bdocker\s+(?:run|rm|stop|restart|compose)\b"),
    )
    _readonly = re.compile(
        r"^\s*(?:sudo\s+)?(?:ls|pwd|cat|head|tail|grep|rg|find|stat|ps|ss|which|"
        r"systemctl\s+(?:status|is-active)|docker\s+(?:ps|inspect)|python\d*\s+-V)\b"
    )
    _unsafe_readonly_shell = re.compile(r"[>|;&`$]")
    _mutating_find_action = re.compile(
        r"(?:^|\s)-(?:delete|exec|execdir|ok|okdir|fprint|fprint0|fprintf|fls)\b"
    )

    def classify_command(self, command: str) -> tuple[str, str]:
        raw = command or ""
        raw_normalized = " ".join(raw.split())
        for pattern in self._hard_denied:
            if pattern.search(raw_normalized):
                return "destructive", "hard-denied catastrophic command"
        if "\n" in raw or "\r" in raw:
            return "low", "multiline command is not allowed in read-only execution"
        try:
            argv = shlex.split(raw, posix=True)
        except ValueError:
            return "low", "command has invalid shell quoting"
        normalized = " ".join(argv)
        for pattern in self._hard_denied:
            if pattern.search(normalized):
                return "destructive", "hard-denied catastrophic command"
        for pattern in self._high:
            if pattern.search(normalized):
                return "high", "deterministic high-risk command pattern"
        for pattern in self._medium:
            if pattern.search(normalized):
                return "medium", "deterministic mutating command pattern"
        if self._readonly.search(normalized):
            if self._unsafe_readonly_shell.search(normalized):
                return "low", "shell evaluation is not allowed in read-only execution"
            if (
                re.match(r"^\s*(?:sudo\s+)?find\b", normalized)
                and self._mutating_find_action.search(normalized)
            ):
                return "low", "mutating find action is not read-only"
            return "readonly", "deterministic read-only command"
        return "low", "unrecognized command treated as mutating"

    def readonly_argv(self, command: str) -> tuple[list[str] | None, str]:
        risk, reason = self.classify_command(command)
        if risk != "readonly":
            return None, reason
        try:
            argv = shlex.split(command, posix=True)
        except ValueError:
            return None, "command has invalid shell quoting"
        if not argv:
            return None, "empty command is not read-only"
        return argv, reason

    def evaluate(self, goal: str, steps: list[PrivilegedStep]) -> RiskDecision:
        del goal
        highest = 0
        denied = False
        reasons = []
        for step in steps:
            deterministic_risk, reason = self.classify_command(step.command)
            effective_index = max(
                RISK_LEVELS.index(step.risk),
                RISK_LEVELS.index(deterministic_risk),
            )
            highest = max(highest, effective_index)
            denied = denied or deterministic_risk == "destructive"
            reasons.append("%s: %s" % (step.step_id, reason))
        risk = RISK_LEVELS[highest] if steps else "readonly"
        if denied:
            return RiskDecision(
                risk=risk,
                denied=True,
                requires_plan_confirmation=True,
                requires_step_confirmation=True,
                reason="hard-denied: " + "; ".join(reasons),
            )
        if risk in {"high", "destructive"}:
            return RiskDecision(
                risk=risk,
                requires_plan_confirmation=True,
                requires_step_confirmation=True,
                reason="; ".join(reasons),
            )
        mutating_steps = [
            step for step in steps if self.classify_command(step.command)[0] != "readonly"
        ]
        reversible = bool(mutating_steps) and all(
            re.search(
                r"\b(?:systemctl|service)\s+(?:restart|start|reload)\b",
                step.command,
            )
            for step in mutating_steps
        )
        if len(steps) == 1 and len(mutating_steps) == 1 and reversible:
            return RiskDecision(
                risk=risk,
                auto_authorized=True,
                reason="single reversible low/medium mutation",
            )
        if mutating_steps:
            return RiskDecision(
                risk=risk,
                requires_plan_confirmation=True,
                reason="multi-step mutating plan",
            )
        return RiskDecision(risk="readonly", auto_authorized=True, reason="read-only")
