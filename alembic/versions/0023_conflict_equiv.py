"""repair conflict consolidation to use proposal equivalence"""

from collections import defaultdict
import re

from alembic import op
import sqlalchemy as sa


revision = "0023_conflict_equiv"
down_revision = "0022_conflict_dedup"
branch_labels = None
depends_on = None

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "in",
        "is",
        "keep",
        "must",
        "of",
        "on",
        "per",
        "the",
        "to",
        "use",
        "with",
    }
)


def _fingerprint(value: str) -> str:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", value.strip().lower())
        if token not in _STOP_WORDS
    }
    return " ".join(sorted(tokens))


def upgrade() -> None:
    bind = op.get_bind()
    op.drop_index("memory_conflicts_one_open_per_key", table_name="memory_conflicts")
    op.add_column(
        "memory_conflicts",
        sa.Column("proposal_fingerprint", sa.Text(), nullable=True),
    )

    rows = list(
        bind.execute(
            sa.text(
                "SELECT id, namespace, decision_key, proposed_value, proposed_rationale, "
                "proposed_project, proposed_source, recurrence_count, last_seen_at, "
                "resolution_decision_id, closed_at "
                "FROM memory_conflicts ORDER BY id"
            )
        ).mappings()
    )
    unresolved = [row for row in rows if row["resolution_decision_id"] is None]
    groups: dict[tuple[str, str, str], list[object]] = defaultdict(list)
    for row in unresolved:
        groups[
            (
                str(row["namespace"]),
                str(row["decision_key"]),
                _fingerprint(str(row["proposed_value"])),
            )
        ].append(row)

    for row in rows:
        fingerprint = _fingerprint(str(row["proposed_value"]))
        bind.execute(
            sa.text(
                "UPDATE memory_conflicts SET proposal_fingerprint = :fingerprint, "
                "latest_proposed_value = proposed_value, "
                "latest_proposed_rationale = proposed_rationale, "
                "latest_proposed_project = proposed_project, "
                "latest_proposed_source = proposed_source "
                "WHERE id = :id"
            ),
            {"id": row["id"], "fingerprint": fingerprint},
        )

    for group in groups.values():
        representative = min(group, key=lambda row: int(row["id"]))
        latest = max(group, key=lambda row: int(row["id"]))
        bind.execute(
            sa.text(
                "UPDATE memory_conflicts SET closed_at = NULL, closure_reason = NULL, "
                "superseded_by_conflict_id = NULL, recurrence_count = :count, "
                "last_seen_at = :last_seen, latest_proposed_value = :value, "
                "latest_proposed_rationale = :rationale, latest_proposed_project = :project, "
                "latest_proposed_source = :source, proposal_fingerprint = :fingerprint "
                "WHERE id = :id"
            ),
            {
                "id": representative["id"],
                "count": sum(int(row["recurrence_count"] or 1) for row in group),
                "last_seen": max(
                    (row["last_seen_at"] for row in group if row["last_seen_at"] is not None),
                    default=latest["last_seen_at"],
                ),
                "value": latest["proposed_value"],
                "rationale": latest["proposed_rationale"],
                "project": latest["proposed_project"],
                "source": latest["proposed_source"],
                "fingerprint": _fingerprint(str(latest["proposed_value"])),
            },
        )
        for row in group:
            if int(row["id"]) == int(representative["id"]):
                continue
            bind.execute(
                sa.text(
                    "UPDATE memory_conflicts SET closed_at = now(), "
                    "closure_reason = 'deduplicated', superseded_by_conflict_id = :canonical "
                    "WHERE id = :id"
                ),
                {"id": row["id"], "canonical": representative["id"]},
            )

    op.alter_column(
        "memory_conflicts",
        "proposal_fingerprint",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.create_check_constraint(
        "memory_conflicts_proposal_fingerprint_ck",
        "memory_conflicts",
        "proposal_fingerprint <> ''",
    )
    op.create_index(
        "memory_conflicts_one_open_per_proposal",
        "memory_conflicts",
        ["namespace", "decision_key", "proposal_fingerprint"],
        unique=True,
        postgresql_where=sa.text(
            "resolution_decision_id IS NULL AND closed_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "memory_conflicts_one_open_per_proposal", table_name="memory_conflicts"
    )
    op.drop_constraint(
        "memory_conflicts_proposal_fingerprint_ck", "memory_conflicts", type_="check"
    )
    op.drop_column("memory_conflicts", "proposal_fingerprint")
    op.create_index(
        "memory_conflicts_one_open_per_key",
        "memory_conflicts",
        ["namespace", "decision_key"],
        unique=True,
        postgresql_where=sa.text(
            "resolution_decision_id IS NULL AND closed_at IS NULL"
        ),
    )
