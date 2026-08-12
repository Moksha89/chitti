"""persist namespace-scoped chat transcripts"""

from alembic import op
import sqlalchemy as sa


revision = "0017_chat_transcripts"
down_revision = "0016_memory_namespaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_transcript_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["namespace"],
            ["memory_namespaces.slug"],
            name="chat_transcript_entries_namespace_fk",
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="chat_transcript_entries_role_ck",
        ),
    )
    op.create_index(
        "chat_transcript_entries_namespace_id_idx",
        "chat_transcript_entries",
        ["namespace", "id"],
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION reject_chat_transcript_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'chat transcripts are append-only'; END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER chat_transcript_entries_immutable
        BEFORE UPDATE OR DELETE ON chat_transcript_entries
        FOR EACH ROW EXECUTE FUNCTION reject_chat_transcript_mutation()"""
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS chat_transcript_entries_immutable "
        "ON chat_transcript_entries"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_chat_transcript_mutation")
    op.drop_index(
        "chat_transcript_entries_namespace_id_idx",
        table_name="chat_transcript_entries",
    )
    op.drop_table("chat_transcript_entries")
