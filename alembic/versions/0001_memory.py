"""phase 1 memory tables"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0001_memory"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("project", sa.String(255), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("decision_key", sa.String(255), nullable=False),
        sa.Column("superseded_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["superseded_by"], ["decisions.id"]),
        sa.CheckConstraint("source IN ('user_stated', 'chitti_inferred')", name="decision_source_ck"),
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION enforce_decisions_append_only() RETURNS trigger AS $$
        BEGIN
          IF NEW.id <> OLD.id OR NEW.ts <> OLD.ts OR NEW.project IS DISTINCT FROM OLD.project
             OR NEW.decision <> OLD.decision OR NEW.rationale IS DISTINCT FROM OLD.rationale
             OR NEW.source <> OLD.source OR NEW.decision_key <> OLD.decision_key
             OR OLD.superseded_by IS NOT NULL OR NEW.superseded_by IS NULL THEN
            RAISE EXCEPTION 'decisions are append-only; only superseded_by may be set once';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER decisions_append_only BEFORE UPDATE ON decisions
        FOR EACH ROW EXECUTE FUNCTION enforce_decisions_append_only()"""
    )
    op.create_table(
        "memory_chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("embedding", Vector(384), nullable=False),
    )
    op.create_index(
        "memory_chunks_embedding_idx",
        "memory_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("memory_chunks_embedding_idx", table_name="memory_chunks")
    op.drop_table("memory_chunks")
    op.execute("DROP TRIGGER IF EXISTS decisions_append_only ON decisions")
    op.execute("DROP FUNCTION IF EXISTS enforce_decisions_append_only")
    op.drop_table("decisions")
