"""track and consolidate repeated memory conflicts"""

from alembic import op
import sqlalchemy as sa


revision = "0022_conflict_dedup"
down_revision = "0021_runner_health_success"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_conflicts",
        sa.Column("recurrence_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "memory_conflicts",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memory_conflicts",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memory_conflicts",
        sa.Column("closure_reason", sa.String(32), nullable=True),
    )
    op.add_column(
        "memory_conflicts",
        sa.Column("superseded_by_conflict_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "memory_conflicts",
        sa.Column("resolution_actor", sa.String(128), nullable=True),
    )
    op.add_column(
        "memory_conflicts",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "memory_conflicts_superseded_by_fk",
        "memory_conflicts",
        "memory_conflicts",
        ["superseded_by_conflict_id"],
        ["id"],
    )
    op.create_check_constraint(
        "memory_conflicts_closure_reason_ck",
        "memory_conflicts",
        "closure_reason IS NULL OR closure_reason IN ('owner', 'deduplicated', 'superseded')",
    )
    op.create_check_constraint(
        "memory_conflicts_recurrence_count_ck",
        "memory_conflicts",
        "recurrence_count >= 1",
    )
    op.execute(
        """
        UPDATE memory_conflicts
        SET last_seen_at = created_at
        WHERE last_seen_at IS NULL
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   first_value(id) OVER (
                       PARTITION BY namespace, decision_key
                       ORDER BY id
                   ) AS canonical_id,
                   row_number() OVER (
                       PARTITION BY namespace, decision_key
                       ORDER BY id
                   ) AS position
            FROM memory_conflicts
            WHERE resolution_decision_id IS NULL AND closed_at IS NULL
        )
        UPDATE memory_conflicts c
        SET closed_at = now(),
            closure_reason = 'deduplicated',
            superseded_by_conflict_id = ranked.canonical_id
        FROM ranked
        WHERE c.id = ranked.id AND ranked.position > 1
        """
    )
    op.create_index(
        "memory_conflicts_one_open_per_key",
        "memory_conflicts",
        ["namespace", "decision_key"],
        unique=True,
        postgresql_where=sa.text(
            "resolution_decision_id IS NULL AND closed_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("memory_conflicts_one_open_per_key", table_name="memory_conflicts")
    op.drop_constraint(
        "memory_conflicts_recurrence_count_ck", "memory_conflicts", type_="check"
    )
    op.drop_constraint(
        "memory_conflicts_closure_reason_ck", "memory_conflicts", type_="check"
    )
    op.drop_constraint(
        "memory_conflicts_superseded_by_fk", "memory_conflicts", type_="foreignkey"
    )
    op.drop_column("memory_conflicts", "resolved_at")
    op.drop_column("memory_conflicts", "resolution_actor")
    op.drop_column("memory_conflicts", "superseded_by_conflict_id")
    op.drop_column("memory_conflicts", "closure_reason")
    op.drop_column("memory_conflicts", "closed_at")
    op.drop_column("memory_conflicts", "last_seen_at")
    op.drop_column("memory_conflicts", "recurrence_count")
