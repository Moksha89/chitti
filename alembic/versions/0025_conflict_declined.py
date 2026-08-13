"""record owner-declined conflict proposals"""

from alembic import op


revision = "0025_conflict_declined"
down_revision = "0024_conflict_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "memory_conflicts_closure_reason_ck", "memory_conflicts", type_="check"
    )
    op.create_check_constraint(
        "memory_conflicts_closure_reason_ck",
        "memory_conflicts",
        "closure_reason IS NULL OR closure_reason IN "
        "('owner', 'owner_reconciled', 'declined', 'deduplicated', 'superseded')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "memory_conflicts_closure_reason_ck", "memory_conflicts", type_="check"
    )
    op.create_check_constraint(
        "memory_conflicts_closure_reason_ck",
        "memory_conflicts",
        "closure_reason IS NULL OR closure_reason IN "
        "('owner', 'owner_reconciled', 'deduplicated', 'superseded')",
    )
