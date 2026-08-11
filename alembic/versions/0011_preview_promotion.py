"""durable static preview manifests and result approvals"""

from alembic import op
import sqlalchemy as sa

revision = "0011_preview_promotion"
down_revision = "0010_model_context_compaction"
branch_labels = None
depends_on = None


def _append_only(table: str, function: str, message: str) -> None:
    op.execute(
        f"""CREATE OR REPLACE FUNCTION {function}() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION '{message}'; END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        f"""CREATE TRIGGER {function}_trigger BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION {function}()"""
    )


def upgrade() -> None:
    op.create_table(
        "export_manifests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("run_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("revision_content_hash", sa.String(64), nullable=False),
        sa.Column("reviewer_artifact_id", sa.Integer(), nullable=False),
        sa.Column("diff_artifact_id", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("max_depth", sa.Integer(), nullable=False),
        sa.Column("staging_path", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["worker_runs.id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["plan_revisions.id"]),
        sa.ForeignKeyConstraint(["reviewer_artifact_id"], ["worker_artifacts.id"]),
        sa.ForeignKeyConstraint(["diff_artifact_id"], ["worker_artifacts.id"]),
        sa.CheckConstraint("total_bytes >= 0", name="export_manifest_size_ck"),
        sa.CheckConstraint("file_count >= 0", name="export_manifest_file_count_ck"),
        sa.CheckConstraint("max_depth >= 0", name="export_manifest_depth_ck"),
    )
    op.create_table(
        "promotion_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("run_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("revision_content_hash", sa.String(64), nullable=False),
        sa.Column("manifest_id", sa.Integer(), nullable=False),
        sa.Column("reviewer_artifact_id", sa.Integer(), nullable=False),
        sa.Column("reviewer_sha256", sa.String(64), nullable=False),
        sa.Column("diff_artifact_id", sa.Integer(), nullable=False),
        sa.Column("diff_sha256", sa.String(64), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["worker_runs.id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["plan_revisions.id"]),
        sa.ForeignKeyConstraint(["manifest_id"], ["export_manifests.id"]),
        sa.ForeignKeyConstraint(["reviewer_artifact_id"], ["worker_artifacts.id"]),
        sa.ForeignKeyConstraint(["diff_artifact_id"], ["worker_artifacts.id"]),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="promotion_approval_decision_ck",
        ),
    )
    op.create_table(
        "previews",
        sa.Column("preview_id", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("manifest_id", sa.Integer(), nullable=False),
        sa.Column("approval_id", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["worker_runs.id"]),
        sa.ForeignKeyConstraint(["manifest_id"], ["export_manifests.id"]),
        sa.ForeignKeyConstraint(["approval_id"], ["promotion_approvals.id"]),
        sa.CheckConstraint("total_bytes >= 0", name="preview_size_ck"),
        sa.CheckConstraint("file_count >= 0", name="preview_file_count_ck"),
    )
    _append_only("export_manifests", "reject_export_manifest_mutation", "export manifests are immutable")
    _append_only("promotion_approvals", "reject_promotion_approval_mutation", "promotion approvals are append-only")
    _append_only("previews", "reject_preview_mutation", "previews are immutable")


def downgrade() -> None:
    for table, function in (
        ("previews", "reject_preview_mutation"),
        ("promotion_approvals", "reject_promotion_approval_mutation"),
        ("export_manifests", "reject_export_manifest_mutation"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {function}_trigger ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
    op.drop_table("previews")
    op.drop_table("promotion_approvals")
    op.drop_table("export_manifests")
