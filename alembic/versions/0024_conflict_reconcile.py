"""record owner reconciliation of sibling conflicts"""

from alembic import op


revision = "0024_conflict_reconcile"
down_revision = "0023_conflict_equiv"
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
        "('owner', 'owner_reconciled', 'deduplicated', 'superseded')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "memory_conflicts_closure_reason_ck", "memory_conflicts", type_="check"
    )
    op.create_check_constraint(
        "memory_conflicts_closure_reason_ck",
        "memory_conflicts",
        "closure_reason IS NULL OR closure_reason IN "
        "('owner', 'deduplicated', 'superseded')",
    )
