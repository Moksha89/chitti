"""record provider stop reasons and alternate message fields"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012_model_call_diagnostics"
down_revision = "0011_preview_promotion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "worker_model_calls",
        sa.Column("finish_reason", sa.String(80), nullable=True),
    )
    op.add_column(
        "worker_model_calls",
        sa.Column(
            "message_fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("worker_model_calls", "message_fields")
    op.drop_column("worker_model_calls", "finish_reason")
