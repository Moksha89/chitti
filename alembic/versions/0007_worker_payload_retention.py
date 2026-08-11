"""separate ephemeral artifact payloads from immutable artifact records"""

from alembic import op
import sqlalchemy as sa

revision = "0007_worker_payload_retention"
down_revision = "0006_worker_cage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("worker_artifacts", "content", nullable=True)
    op.create_table(
        "worker_artifact_payloads",
        sa.Column("artifact_id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["worker_artifacts.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "worker_retention_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("max_payload_bytes", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute(
        "INSERT INTO worker_retention_policy (id, max_payload_bytes) "
        "VALUES (1, 524288000)"
    )


def downgrade() -> None:
    op.drop_table("worker_retention_policy")
    op.drop_table("worker_artifact_payloads")
    op.alter_column("worker_artifacts", "content", nullable=False)
