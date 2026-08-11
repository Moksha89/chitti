"""persist embeddings for belief-slot matching"""

import os

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from chitti.embedding import get_embedder, vector_literal
from chitti.memory import belief_subject, normalize_key

revision = "0004_decision_embeddings"
down_revision = "0003_forget_markers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_embeddings",
        sa.Column("decision_id", sa.Integer(), primary_key=True),
        sa.Column("key_normalized", sa.String(255), nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.id"]),
    )
    op.create_index(
        "decision_embeddings_embedding_idx",
        "decision_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "decision_embeddings_key_idx",
        "decision_embeddings",
        ["key_normalized"],
    )
    connection = op.get_bind()
    embedder = get_embedder(
        os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    )
    decisions = connection.execute(
        sa.text("SELECT id, decision_key, decision FROM decisions ORDER BY id")
    )
    for decision in decisions:
        key = str(decision.decision_key)
        value = str(decision.decision)
        normalized_key = normalize_key(key)
        connection.execute(
            sa.text(
                "INSERT INTO decision_embeddings (decision_id, key_normalized, embedding) "
                "VALUES (:id, :key, CAST(:embedding AS vector))"
            ),
            {
                "id": decision.id,
                "key": normalized_key,
                "embedding": vector_literal(embedder.embed(belief_subject(key, value))),
            },
        )


def downgrade() -> None:
    op.drop_index("decision_embeddings_key_idx", table_name="decision_embeddings")
    op.drop_index("decision_embeddings_embedding_idx", table_name="decision_embeddings")
    op.drop_table("decision_embeddings")
