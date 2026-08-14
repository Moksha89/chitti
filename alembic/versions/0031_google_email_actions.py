"""add exact-content approved Google email actions"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0031_google_email_actions"
down_revision = "0030_google_read_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "google_provider_accounts_status_ck",
        "google_provider_accounts",
        type_="check",
    )
    op.create_check_constraint(
        "google_provider_accounts_status_ck",
        "google_provider_accounts",
        "status IN ('connected', 'error', 'reconnect_needed')",
    )
    op.create_table(
        "google_email_actions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("to_recipients", JSONB(), nullable=False),
        sa.Column("cc_recipients", JSONB(), nullable=False),
        sa.Column("bcc_recipients", JSONB(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("attachments", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("execution_state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["google_provider_accounts.id"]),
        sa.ForeignKeyConstraint(["namespace"], ["memory_namespaces.slug"]),
        sa.CheckConstraint("action_type = 'gmail.send'", name="google_email_action_type_ck"),
        sa.CheckConstraint(
            "state IN ('pending', 'sending', 'sent', 'failed', 'rejected')",
            name="google_email_action_state_ck",
        ),
        sa.CheckConstraint(
            "execution_state IN ('pending', 'claimed', 'succeeded', 'failed', 'rejected')",
            name="google_email_action_execution_ck",
        ),
    )
    op.create_index(
        "google_email_actions_namespace_state_idx",
        "google_email_actions",
        ["namespace", "state", "created_at"],
    )
    op.create_table(
        "google_email_action_approvals",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("action_id", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("action_id", name="google_email_action_one_decision_uk"),
        sa.ForeignKeyConstraint(["action_id"], ["google_email_actions.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="google_email_approval_decision_ck",
        ),
    )
    op.create_index(
        "google_email_action_approvals_action_idx",
        "google_email_action_approvals",
        ["action_id", "created_at"],
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION reject_google_email_action_mutation() RETURNS trigger AS $$
        BEGIN
          IF NEW.namespace IS DISTINCT FROM OLD.namespace
             OR NEW.account_id IS DISTINCT FROM OLD.account_id
             OR NEW.action_type IS DISTINCT FROM OLD.action_type
             OR NEW.to_recipients IS DISTINCT FROM OLD.to_recipients
             OR NEW.cc_recipients IS DISTINCT FROM OLD.cc_recipients
             OR NEW.bcc_recipients IS DISTINCT FROM OLD.bcc_recipients
             OR NEW.subject IS DISTINCT FROM OLD.subject
             OR NEW.body IS DISTINCT FROM OLD.body
             OR NEW.attachments IS DISTINCT FROM OLD.attachments
             OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
             OR NEW.requested_by IS DISTINCT FROM OLD.requested_by
             OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
             OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
          THEN RAISE EXCEPTION 'Google email action content is immutable'; END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER reject_google_email_action_mutation_trigger
        BEFORE UPDATE ON google_email_actions FOR EACH ROW
        EXECUTE FUNCTION reject_google_email_action_mutation()"""
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION reject_google_email_approval_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'Google email approvals are append-only'; END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER reject_google_email_approval_mutation_trigger
        BEFORE UPDATE OR DELETE ON google_email_action_approvals FOR EACH ROW
        EXECUTE FUNCTION reject_google_email_approval_mutation()"""
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS reject_google_email_approval_mutation_trigger "
        "ON google_email_action_approvals"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_google_email_approval_mutation")
    op.execute(
        "DROP TRIGGER IF EXISTS reject_google_email_action_mutation_trigger "
        "ON google_email_actions"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_google_email_action_mutation")
    op.drop_index(
        "google_email_action_approvals_action_idx",
        table_name="google_email_action_approvals",
    )
    op.drop_table("google_email_action_approvals")
    op.drop_index("google_email_actions_namespace_state_idx", table_name="google_email_actions")
    op.drop_table("google_email_actions")
    op.drop_constraint(
        "google_provider_accounts_status_ck",
        "google_provider_accounts",
        type_="check",
    )
    op.create_check_constraint(
        "google_provider_accounts_status_ck",
        "google_provider_accounts",
        "status IN ('connected', 'error')",
    )
