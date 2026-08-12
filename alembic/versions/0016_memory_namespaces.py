"""enforce explicit memory namespace boundaries"""

from alembic import op
import sqlalchemy as sa

from chitti.namespaces import MEMORY_NAMESPACES, NAMESPACE_ROWS, SHARED_NAMESPACE


revision = "0016_memory_namespaces"
down_revision = "0015_approval_actor"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "memory_namespaces",
        sa.Column("slug", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("is_shared", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.bulk_insert(
        sa.table(
            "memory_namespaces",
            sa.column("slug", sa.String(64)),
            sa.column("display_name", sa.String(128)),
            sa.column("is_shared", sa.Boolean()),
        ),
        list(NAMESPACE_ROWS),
    )

    for table_name in ("memory_chunks", "decisions", "memory_conflicts", "plan_revisions", "plan_jobs"):
        op.add_column(
            table_name,
            sa.Column(
                "namespace",
                sa.String(64),
                nullable=False,
                server_default=SHARED_NAMESPACE,
            ),
        )
        op.create_foreign_key(
            f"{table_name}_namespace_fk",
            table_name,
            "memory_namespaces",
            ["namespace"],
            ["slug"],
        )

    valid_slugs = ", ".join(f"'{slug}'" for slug in MEMORY_NAMESPACES)
    op.execute(
        f"""
        UPDATE memory_chunks
        SET namespace = CASE
            WHEN metadata ->> 'namespace' IN
                ({valid_slugs})
            THEN metadata ->> 'namespace'
            ELSE '{SHARED_NAMESPACE}'
        END
        """
    )


def downgrade() -> None:
    for table_name in ("plan_jobs", "plan_revisions", "memory_conflicts", "decisions", "memory_chunks"):
        op.drop_constraint(f"{table_name}_namespace_fk", table_name, type_="foreignkey")
        op.drop_column(table_name, "namespace")
    op.drop_table("memory_namespaces")
