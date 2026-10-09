"""治理层证据 / 主张 / 路由决策的 PostgreSQL 投影写入（0005 迁移的表）。

作为 ``PostgresGovernanceRepository`` 的 mixin 实现——事件与这些投影
仍在**同一个事务**里提交（append_event / record_* 返回 False 即幂等命中，
投影不重复应用）。
"""

from __future__ import annotations

import json
from typing import Any

from klonet_agent.runtime.governance.models import (
    enum_text,
    RuntimeEvent,
)
from klonet_agent.runtime.governance.provenance import (
    ClaimEvidenceLink,
    ClaimRecord,
    ClaimStatus,
    EvidenceRecord,
)
from klonet_agent.runtime.governance.routing_policy import RouteDecision


def _jsonb(value: Any) -> Any:
    return json.dumps(value, ensure_ascii=False, default=str)


class GovernanceEvidenceMixin:
    """混入 PostgresGovernanceRepository 的证据/主张/路由写入。"""

    def add_evidence(self, evidence: EvidenceRecord, event: RuntimeEvent) -> bool:
        row = self._event_row(event)
        with self.database.tenant_session(self.tenant) as conn:
            self._ensure_turn_row(conn, event)
            inserted = conn.execute(
                """
                INSERT INTO governance.runtime_events
                    (event_id, event_type, schema_version, user_id, project_id,
                     session_id, run_id, turn_id, idempotency_key, actor_type,
                     actor_id, payload, privacy_class, occurred_at)
                VALUES (%(event_id)s, %(event_type)s, %(schema_version)s,
                        %(user_id)s, %(project_id)s, %(session_id)s,
                        %(run_id)s, %(turn_id)s, %(idempotency_key)s,
                        %(actor_type)s, %(actor_id)s, %(payload)s,
                        %(privacy_class)s, %(occurred_at)s)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                row,
            ).fetchone()
            if inserted is None:
                return False
            conn.execute(
                """
                INSERT INTO governance.evidence
                    (evidence_id, run_id, user_id, project_id, source_type,
                     source_uri, source_revision, subject, observation,
                     observed_at, valid_from, valid_until, scope, confidence,
                     freshness, artifact_hash, producer, parent_evidence_ids,
                     privacy_class, idempotency_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (evidence_id) DO NOTHING
                """,
                (
                    evidence.evidence_id,
                    evidence.run_id,
                    evidence.user_id,
                    evidence.project_id,
                    enum_text(evidence.source_type),
                    evidence.source_uri,
                    evidence.source_revision,
                    evidence.subject,
                    evidence.observation,
                    evidence.observed_at,
                    evidence.valid_from or evidence.observed_at,
                    evidence.valid_until,
                    enum_text(evidence.scope),
                    evidence.confidence,
                    evidence.freshness,
                    evidence.artifact_hash,
                    evidence.producer,
                    _jsonb(evidence.parent_evidence_ids),
                    enum_text(event.privacy_class),
                    evidence.idempotency_key,
                ),
            )
        return True

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        with self.database.tenant_session(self.tenant, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM governance.evidence WHERE evidence_id = %s",
                (evidence_id,),
            ).fetchone()
        return self._evidence_from_row(row) if row else None

    def find_evidence_by_subject(self, subject: str) -> list[EvidenceRecord]:
        with self.database.tenant_session(self.tenant, readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT * FROM governance.evidence
                 WHERE subject = %s AND freshness <> 'stale'
                 ORDER BY observed_at DESC
                 LIMIT 50
                """,
                (subject,),
            ).fetchall()
        return [self._evidence_from_row(row) for row in rows]

    def mark_evidence_stale(self, evidence_id: str, event: RuntimeEvent) -> None:
        row = self._event_row(event)
        with self.database.tenant_session(self.tenant) as conn:
            self._ensure_turn_row(conn, event)
            conn.execute(
                """
                INSERT INTO governance.runtime_events
                    (event_id, event_type, schema_version, user_id, project_id,
                     session_id, run_id, turn_id, idempotency_key, actor_type,
                     actor_id, payload, privacy_class, occurred_at)
                VALUES (%(event_id)s, %(event_type)s, %(schema_version)s,
                        %(user_id)s, %(project_id)s, %(session_id)s,
                        %(run_id)s, %(turn_id)s, %(idempotency_key)s,
                        %(actor_type)s, %(actor_id)s, %(payload)s,
                        %(privacy_class)s, %(occurred_at)s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                row,
            )
            conn.execute(
                """
                UPDATE governance.evidence
                   SET freshness = 'stale', valid_until = now(), updated_at = now()
                 WHERE evidence_id = %s
                """,
                (evidence_id,),
            )

    def add_claim(self, claim: ClaimRecord, event: RuntimeEvent) -> bool:
        row = self._event_row(event)
        with self.database.tenant_session(self.tenant) as conn:
            self._ensure_turn_row(conn, event)
            inserted = conn.execute(
                """
                INSERT INTO governance.runtime_events
                    (event_id, event_type, schema_version, user_id, project_id,
                     session_id, run_id, turn_id, task_id, idempotency_key,
                     actor_type, actor_id, payload, privacy_class, occurred_at)
                VALUES (%(event_id)s, %(event_type)s, %(schema_version)s,
                        %(user_id)s, %(project_id)s, %(session_id)s,
                        %(run_id)s, %(turn_id)s, %(task_id)s,
                        %(idempotency_key)s, %(actor_type)s, %(actor_id)s,
                        %(payload)s, %(privacy_class)s, %(occurred_at)s)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                {
                    **row,
                    "task_id": claim.task_id,
                },
            ).fetchone()
            if inserted is None:
                return False
            conn.execute(
                """
                INSERT INTO governance.claims
                    (claim_id, run_id, user_id, project_id, subject, statement,
                     status, confidence, task_id, turn_id, idempotency_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (claim_id) DO NOTHING
                """,
                (
                    claim.claim_id,
                    claim.run_id,
                    claim.user_id,
                    claim.project_id,
                    claim.subject,
                    claim.statement,
                    enum_text(claim.status),
                    claim.confidence,
                    claim.task_id,
                    claim.turn_id,
                    claim.idempotency_key,
                ),
            )
        return True

    def link_claim_evidence(self, link: ClaimEvidenceLink, event: RuntimeEvent) -> bool:
        row = self._event_row(event)
        with self.database.tenant_session(self.tenant) as conn:
            self._ensure_turn_row(conn, event)
            inserted = conn.execute(
                """
                INSERT INTO governance.runtime_events
                    (event_id, event_type, schema_version, user_id, project_id,
                     session_id, run_id, turn_id, idempotency_key, actor_type,
                     actor_id, payload, privacy_class, occurred_at)
                VALUES (%(event_id)s, %(event_type)s, %(schema_version)s,
                        %(user_id)s, %(project_id)s, %(session_id)s,
                        %(run_id)s, %(turn_id)s, %(idempotency_key)s,
                        %(actor_type)s, %(actor_id)s, %(payload)s,
                        %(privacy_class)s, %(occurred_at)s)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                row,
            ).fetchone()
            if inserted is None:
                return False
            conn.execute(
                """
                INSERT INTO governance.claim_evidence
                    (claim_id, evidence_id, user_id, project_id, relation, weight)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (claim_id, evidence_id, relation) DO NOTHING
                """,
                (
                    link.claim_id,
                    link.evidence_id,
                    self.tenant.user_id,
                    self.tenant.project_id,
                    enum_text(link.relation),
                    link.weight,
                ),
            )
        return True

    def list_claim_links(self, claim_id: str) -> list[ClaimEvidenceLink]:
        with self.database.tenant_session(self.tenant, readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT claim_id, evidence_id, relation, weight
                  FROM governance.claim_evidence
                 WHERE claim_id = %s
                """,
                (claim_id,),
            ).fetchall()
        return [
            ClaimEvidenceLink(
                claim_id=row["claim_id"],
                evidence_id=row["evidence_id"],
                relation=row["relation"],
                weight=float(row["weight"]),
            )
            for row in rows
        ]

    def list_claims(self, run_id: str) -> list[ClaimRecord]:
        with self.database.tenant_session(self.tenant, readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM governance.claims WHERE run_id = %s ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [self._claim_from_row(row) for row in rows]

    def record_route_decision(self, decision: RouteDecision, event: RuntimeEvent) -> bool:
        row = self._event_row(event)
        with self.database.tenant_session(self.tenant) as conn:
            self._ensure_turn_row(conn, event)
            inserted = conn.execute(
                """
                INSERT INTO governance.runtime_events
                    (event_id, event_type, schema_version, user_id, project_id,
                     session_id, run_id, turn_id, idempotency_key, actor_type,
                     actor_id, payload, privacy_class, occurred_at)
                VALUES (%(event_id)s, %(event_type)s, %(schema_version)s,
                        %(user_id)s, %(project_id)s, %(session_id)s,
                        %(run_id)s, %(turn_id)s, %(idempotency_key)s,
                        %(actor_type)s, %(actor_id)s, %(payload)s,
                        %(privacy_class)s, %(occurred_at)s)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                row,
            ).fetchone()
            if inserted is None:
                return False
            conn.execute(
                """
                INSERT INTO governance.route_decisions
                    (decision_id, run_id, user_id, project_id, task_level,
                     selected, actual_model, shadow, policy_version,
                     reason_codes, candidate_scores, excluded, occurred_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (decision_id) DO NOTHING
                """,
                (
                    decision.decision_id,
                    decision.run_id,
                    decision.user_id,
                    decision.project_id,
                    decision.task_level,
                    decision.selected,
                    decision.actual_model,
                    decision.shadow,
                    decision.policy_version,
                    _jsonb(decision.reason_codes),
                    _jsonb(decision.candidate_scores),
                    _jsonb(decision.excluded),
                    decision.occurred_at,
                ),
            )
        return True

    # ---------------------------------------------------------------- 行映射 --
    @staticmethod
    def _evidence_from_row(row: Any) -> EvidenceRecord:
        parent_ids = row["parent_evidence_ids"]
        if isinstance(parent_ids, str):
            parent_ids = json.loads(parent_ids)
        return EvidenceRecord(
            evidence_id=row["evidence_id"],
            run_id=row["run_id"],
            user_id=row["user_id"],
            project_id=row["project_id"],
            source_type=row["source_type"],
            source_uri=row["source_uri"],
            subject=row["subject"],
            observation=row["observation"],
            source_revision=row["source_revision"],
            observed_at=row["observed_at"],
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            scope=row["scope"],
            confidence=float(row["confidence"]),
            freshness=row["freshness"],
            artifact_hash=row["artifact_hash"],
            producer=row["producer"],
            parent_evidence_ids=list(parent_ids or []),
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _claim_from_row(row: Any) -> ClaimRecord:
        return ClaimRecord(
            claim_id=row["claim_id"],
            run_id=row["run_id"],
            user_id=row["user_id"],
            project_id=row["project_id"],
            subject=row["subject"],
            statement=row["statement"],
            status=row["status"],
            confidence=float(row["confidence"]),
            task_id=row["task_id"],
            turn_id=row["turn_id"],
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
        )
