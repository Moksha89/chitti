"""record provider reasoning-token usage"""

from alembic import op
import sqlalchemy as sa


revision = "0013_reasoning_token_accounting"
down_revision = "0012_model_call_diagnostics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "worker_model_calls",
        sa.Column(
            "reasoning_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "worker_model_call_reasoning_tokens_ck",
        "worker_model_calls",
        "reasoning_tokens >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "worker_model_call_reasoning_tokens_ck",
        "worker_model_calls",
        type_="check",
    )
    op.drop_column("worker_model_calls", "reasoning_tokens")
