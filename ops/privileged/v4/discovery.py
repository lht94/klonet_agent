"""Bounded read-only evidence collection for Ops-Privilege V4."""

from __future__ import annotations

import json
from typing import Any, Callable

from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES
from klonet_agent.ops.privileged.v4.contracts import (
    DiscoveryBudget,
    DiscoveryBudgetExceeded,
    EvidenceBundle,
    EvidenceRecord,
    ProbeRequest,
)


DISCOVERY_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege V4 Discovery Agent.
You may request only registered read-only probes. Never propose actions, shell,
configuration changes, plans, or user confirmation.

Return one JSON object with status `need_evidence`, `ready`, or `blocked`.
For need_evidence return at most four probe_requests with probe, args, purpose.
Request only facts materially needed for the current goal. Do not repeat an
identical probe and arguments after evidence was returned.

When the user identifies a source instance by a Screen session name or prefix,
treat matching `screen_runtime` cwd/git_roots evidence as authoritative for
that source. Do not replace it with an unrelated path found by a broad runtime
or process probe. If a candidate path is not a Git repository, try the matching
Screen git_root or cwd ancestors before returning blocked.
Once a matching `screen_git_repositories` section reports
inside_work_tree=true with remote and branch, source discovery is complete.
Do not request screen_session or broad process probes to rediscover that source.

Registered probes:
%s
""".strip()


def parse_json_object(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("response does not contain JSON")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("response JSON must be an object")
    return value


class V4DiscoveryAgent:
    def __init__(
        self,
        llm: Any,
        *,
        probe_runner: Callable[[list[dict[str, Any]]], str] | None,
        readonly_command_runner: Callable[[str], str] | None = None,
        budget_factory: Callable[[], DiscoveryBudget] = DiscoveryBudget,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.llm = llm
        self.probe_runner = probe_runner
        self.readonly_command_runner = readonly_command_runner
        self.budget_factory = budget_factory
        self.on_progress = on_progress

    def collect_requests(
        self,
        requests: list[ProbeRequest],
        bundle: EvidenceBundle,
    ) -> EvidenceBundle:
        """Collect a bounded Planner-requested evidence increment."""

        known_keys = {item.request.cache_key for item in bundle.records}
        fresh: list[ProbeRequest] = []
        for request in requests:
            if request.cache_key in known_keys:
                continue
            if DEFAULT_READONLY_PROBES.get(request.probe) is None:
                bundle.add(
                    EvidenceRecord.from_probe(
                        request,
                        "probe refused: probe_not_registered=%s" % request.probe,
                        status="unavailable",
                    )
                )
                known_keys.add(request.cache_key)
                continue
            fresh.append(request)
            known_keys.add(request.cache_key)
        if len(fresh) > 4:
            bundle.budget_exhausted = True
            fresh = fresh[:4]
        for request in fresh:
            if self.on_progress is not None:
                self.on_progress("collecting read-only evidence: %s" % request.probe)
            try:
                output = (
                    self.probe_runner(
                        [{"probe": request.probe, "args": request.args, "purpose": request.purpose}]
                    )
                    if self.probe_runner is not None
                    else "probe runner unavailable"
                )
                status = "available" if self.probe_runner is not None else "unavailable"
            except Exception as exc:
                output = "probe failed: %s" % type(exc).__name__
                status = "unavailable"
            bundle.add(EvidenceRecord.from_probe(request, output, status=status))
        return bundle

    def collect(
        self,
        goal: str,
        *,
        command: str = "",
        conversation_context: str = "",
    ) -> EvidenceBundle:
        bundle = EvidenceBundle(goal=goal)
        if command.strip():
            request = ProbeRequest(
                "readonly_command",
                {"command": command.strip()},
                "execute classifier-provided deterministic read-only command",
            )
            try:
                output = (
                    self.readonly_command_runner(command.strip())
                    if self.readonly_command_runner is not None
                    else "read-only command boundary unavailable"
                )
                status = (
                    "available"
                    if self.readonly_command_runner is not None
                    else "unavailable"
                )
            except Exception as exc:
                output = "read-only command failed: %s" % type(exc).__name__
                status = "unavailable"
            bundle.add(EvidenceRecord.from_probe(request, output, status=status))
            return bundle

        budget = self.budget_factory()
        messages = [
            {
                "role": "system",
                "content": DISCOVERY_SYSTEM_PROMPT % DEFAULT_READONLY_PROBES.render(),
            },
            {
                "role": "user",
                "content": "Recent conversation:\n%s\n\nCurrent goal:\n%s"
                % (conversation_context or "(none)", goal),
            },
        ]
        invalid_repairs = 0
        while True:
            try:
                data = parse_json_object(self._complete(messages))
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                if invalid_repairs >= 1:
                    bundle.blocked_reason = "discovery output invalid: %s" % exc
                    return bundle
                invalid_repairs += 1
                messages.append(
                    {
                        "role": "user",
                        "content": "Return one valid Discovery JSON object. Error: %s"
                        % exc,
                    }
                )
                continue
            status = str(data.get("status") or "").strip().lower()
            if status == "ready":
                return bundle
            if status == "blocked":
                bundle.blocked_reason = str(data.get("reason") or "discovery blocked")
                return bundle
            if status != "need_evidence":
                messages.append(
                    {
                        "role": "user",
                        "content": "status must be need_evidence, ready, or blocked",
                    }
                )
                invalid_repairs += 1
                if invalid_repairs > 1:
                    bundle.blocked_reason = "invalid discovery status"
                    return bundle
                continue
            requests = self._requests(data.get("probe_requests"))
            try:
                fresh = budget.register_round(requests)
            except DiscoveryBudgetExceeded:
                bundle.budget_exhausted = True
                return bundle
            evidence_sections = []
            for request in fresh:
                if self.on_progress is not None:
                    self.on_progress("正在执行只读检查：%s" % request.probe)
                try:
                    output = (
                        self.probe_runner(
                            [
                                {
                                    "probe": request.probe,
                                    "args": request.args,
                                    "purpose": request.purpose,
                                }
                            ]
                        )
                        if self.probe_runner is not None
                        else "probe runner unavailable"
                    )
                    status_value = (
                        "available" if self.probe_runner is not None else "unavailable"
                    )
                except Exception as exc:
                    output = "probe failed: %s" % type(exc).__name__
                    status_value = "unavailable"
                record = bundle.add(
                    EvidenceRecord.from_probe(
                        request,
                        output,
                        status=status_value,
                    )
                )
                evidence_sections.append(
                    "%s (%s):\n%s"
                    % (record.evidence_id, request.probe, record.output[:7000])
                )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Read-only evidence for this round:\n%s\n\n"
                        "Continue discovery. Reuse previously returned evidence and "
                        "return ready when facts are sufficient."
                    )
                    % ("\n\n".join(evidence_sections) or "No new evidence; duplicates were reused."),
                }
            )

    def _complete(self, messages: list[dict[str, str]]) -> str:
        response = self.llm.complete(
            messages=messages,
            tools=None,
            reasoning_effort="low",
            temperature=0,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return response.choices[0].message.content or ""

    @staticmethod
    def _requests(value: Any) -> list[ProbeRequest]:
        if not isinstance(value, list):
            return []
        requests = []
        known_keys: set[str] = set()
        aliases = {
            "screen_runtime": "screen",
            "screen_git_repositories": "screen",
        }
        for item in value:
            if not isinstance(item, dict):
                continue
            requested_probe = str(item.get("probe") or "").strip()
            request = ProbeRequest(
                probe=aliases.get(requested_probe, requested_probe),
                args=item.get("args") if isinstance(item.get("args"), dict) else {},
                purpose=str(item.get("purpose") or "collect required fact"),
            )
            if request.cache_key in known_keys:
                continue
            known_keys.add(request.cache_key)
            requests.append(request)
        return requests
