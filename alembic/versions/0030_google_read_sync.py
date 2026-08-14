"""add namespace-scoped Google read synchronization"""

from alembic import op
import sqlalchemy as sa


revision = "0030_google_read_sync"
down_revision = "0029_generated_images"
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
        "google_provider_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("google_email", sa.String(320), nullable=False),
        sa.Column("google_subject", sa.String(255), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="connected"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["namespace"], ["memory_namespaces.slug"]),
        sa.UniqueConstraint("namespace", "google_email", name="google_provider_accounts_namespace_email_uq"),
        sa.CheckConstraint("status IN ('connected', 'error')", name="google_provider_accounts_status_ck"),
    )
    op.create_table(
        "google_oauth_credentials",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("client_config_ciphertext", sa.Text(), nullable=False),
        sa.Column("refresh_token_ciphertext", sa.Text(), nullable=False),
        sa.Column("key_version", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["google_provider_accounts.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "google_sync_state",
        sa.Column("account_id", sa.BigInteger(), primary_key=True),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("gmail_history_id", sa.String(64), nullable=True),
        sa.Column("gmail_full_sync_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("calendar_sync_token", sa.Text(), nullable=True),
        sa.Column("last_sync_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["google_provider_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["namespace"], ["memory_namespaces.slug"]),
    )
    op.create_table(
        "google_gmail_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("gmail_message_id", sa.String(255), nullable=False),
        sa.Column("thread_id", sa.String(255), nullable=True),
        sa.Column("history_id", sa.String(64), nullable=True),
        sa.Column("internal_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sender", sa.Text(), nullable=True),
        sa.Column("recipients", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["google_provider_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["namespace"], ["memory_namespaces.slug"]),
        sa.UniqueConstraint("account_id", "gmail_message_id", name="google_gmail_messages_account_message_uq"),
    )
    op.create_index(
        "google_gmail_messages_namespace_date_idx",
        "google_gmail_messages",
        ["namespace", "internal_date"],
    )
    op.create_table(
        "google_calendar_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("calendar_id", sa.String(255), nullable=False),
        sa.Column("event_id", sa.String(255), nullable=False),
        sa.Column("etag", sa.String(255), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("html_link", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["google_provider_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["namespace"], ["memory_namespaces.slug"]),
        sa.UniqueConstraint("account_id", "calendar_id", "event_id", name="google_calendar_events_account_event_uq"),
    )
    op.create_index(
        "google_calendar_events_namespace_start_idx",
        "google_calendar_events",
        ["namespace", "start_at"],
    )
    op.create_table(
        "google_account_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["namespace"], ["memory_namespaces.slug"]),
    )
    _append_only("google_account_audit", "reject_google_account_audit_mutation", "Google account audit is append-only")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS reject_google_account_audit_mutation_trigger ON google_account_audit")
    op.execute("DROP FUNCTION IF EXISTS reject_google_account_audit_mutation")
    op.drop_table("google_account_audit")
    op.drop_index("google_calendar_events_namespace_start_idx", table_name="google_calendar_events")
    op.drop_table("google_calendar_events")
    op.drop_index("google_gmail_messages_namespace_date_idx", table_name="google_gmail_messages")
    op.drop_table("google_gmail_messages")
    op.drop_table("google_sync_state")
    op.drop_table("google_oauth_credentials")
    op.drop_table("google_provider_accounts")
