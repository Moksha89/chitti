"""record original and truncated model artifact sizes"""

from alembic import op
import sqlalchemy as sa

revision = "0009_model_artifact_sizes"
down_revision = "0008_worker_model_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "worker_artifacts",
        sa.Column("original_byte_size", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "worker_artifacts",
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("worker_artifacts", "truncated")
    op.drop_column("worker_artifacts", "original_byte_size")
