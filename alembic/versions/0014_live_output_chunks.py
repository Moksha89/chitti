"""persist bounded transient live operation output chunks"""

from alembic import op
import sqlalchemy as sa


revision = "0014_live_output_chunks"
down_revision = "0013_reasoning_token_accounting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_operation_output_chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("operation_index", sa.Integer(), nullable=False),
        sa.Column("stream", sa.String(16), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("byte_offset", sa.BigInteger(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["worker_runs.id"]),
        sa.CheckConstraint("stream IN ('stdout','stderr')", name="live_output_stream_ck"),
        sa.CheckConstraint("sequence >= 0", name="live_output_sequence_ck"),
        sa.CheckConstraint("byte_offset >= 0", name="live_output_offset_ck"),
        sa.CheckConstraint("octet_length(content) > 0", name="live_output_content_ck"),
        sa.UniqueConstraint(
            "run_id",
            "operation_index",
            "stream",
            "sequence",
            name="live_output_chunk_identity_uq",
        ),
    )
    op.create_index(
        "live_output_run_cursor_idx",
        "worker_operation_output_chunks",
        ["run_id", "id"],
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_live_output_chunk_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'live output chunks are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_live_output_chunk_mutation_trigger
        BEFORE UPDATE ON worker_operation_output_chunks
        FOR EACH ROW EXECUTE FUNCTION reject_live_output_chunk_mutation()
        """
    )
    op.execute(
        """
        ALTER TABLE worker_run_events
        DROP CONSTRAINT worker_run_event_status_ck
        """
    )
    op.create_check_constraint(
        "worker_run_event_status_ck",
        "worker_run_events",
        "status IN ('queued','running','operation_running','operation_complete',"
        "'passed','failed','cancel_requested','cancelled','task_finished',"
        "'model_tool_running','model_tool_failed','model_route_switched',"
        "'model_context_compacted','review_complete')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "worker_run_event_status_ck",
        "worker_run_events",
        type_="check",
    )
    op.create_check_constraint(
        "worker_run_event_status_ck",
        "worker_run_events",
        "status IN ('queued','running','operation_running','passed','failed',"
        "'cancel_requested','cancelled','task_finished','model_tool_failed',"
        "'model_route_switched','model_context_compacted','review_complete')",
    )
    op.execute(
        "DROP TRIGGER IF EXISTS reject_live_output_chunk_mutation_trigger "
        "ON worker_operation_output_chunks"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS reject_live_output_chunk_mutation"
    )
    op.drop_index(
        "live_output_run_cursor_idx",
        table_name="worker_operation_output_chunks",
    )
    op.drop_table("worker_operation_output_chunks")
