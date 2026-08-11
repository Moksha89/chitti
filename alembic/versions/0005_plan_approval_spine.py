"""append-only planning and approval history"""

from alembic import op
import sqlalchemy as sa

revision = "0005_plan_approval_spine"
down_revision = "0004_decision_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("project", sa.String(255), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("parent_revision_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["parent_revision_id"], ["plan_revisions.id"]),
        sa.UniqueConstraint("project", "revision"),
    )
    op.create_table(
        "plan_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("project", sa.String(255), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("parent_revision_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("revision_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["parent_revision_id"], ["plan_revisions.id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["plan_revisions.id"]),
        sa.CheckConstraint("status IN ('queued', 'running', 'complete', 'failed')", name="plan_job_status_ck"),
    )
    op.create_table(
        "plan_task_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["revision_id"], ["plan_revisions.id"]),
    )
    op.create_table(
        "plan_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["revision_id"], ["plan_revisions.id"]),
        sa.CheckConstraint("decision IN ('approved', 'rejected')", name="plan_approval_decision_ck"),
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION reject_plan_revision_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'plan revisions are immutable'; END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER plan_revisions_immutable BEFORE UPDATE OR DELETE ON plan_revisions
        FOR EACH ROW EXECUTE FUNCTION reject_plan_revision_mutation()"""
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION reject_plan_event_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'plan task events are append-only'; END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER plan_task_events_immutable BEFORE UPDATE OR DELETE ON plan_task_events
        FOR EACH ROW EXECUTE FUNCTION reject_plan_event_mutation()"""
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION reject_plan_approval_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'plan approvals are append-only'; END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER plan_approvals_immutable BEFORE UPDATE OR DELETE ON plan_approvals
        FOR EACH ROW EXECUTE FUNCTION reject_plan_approval_mutation()"""
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS plan_approvals_immutable ON plan_approvals")
    op.execute("DROP FUNCTION IF EXISTS reject_plan_approval_mutation")
    op.execute("DROP TRIGGER IF EXISTS plan_task_events_immutable ON plan_task_events")
    op.execute("DROP FUNCTION IF EXISTS reject_plan_event_mutation")
    op.execute("DROP TRIGGER IF EXISTS plan_revisions_immutable ON plan_revisions")
    op.execute("DROP FUNCTION IF EXISTS reject_plan_revision_mutation")
    op.drop_table("plan_approvals")
    op.drop_table("plan_task_events")
    op.drop_table("plan_jobs")
    op.drop_table("plan_revisions")
