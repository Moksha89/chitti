"""durable namespace-scoped reminders and notifications"""

from alembic import op
import sqlalchemy as sa


revision = "0018_reminders_notifications"
down_revision = "0017_chat_transcripts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recurrence", sa.String(16), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["namespace"], ["memory_namespaces.slug"]),
        sa.CheckConstraint(
            "recurrence IS NULL OR recurrence IN ('daily', 'weekly')",
            name="reminders_recurrence_ck",
        ),
    )
    op.create_index(
        "reminders_due_idx", "reminders", ["active", "due_at", "namespace"]
    )
    op.create_table(
        "reminder_occurrences",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("reminder_id", sa.BigInteger(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["reminder_id"], ["reminders.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("reminder_id", "due_at", name="reminder_occurrence_due_uq"),
    )
    op.create_index(
        "reminder_occurrences_due_idx",
        "reminder_occurrences",
        ["reminder_id", "due_at"],
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reminder_occurrence_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["namespace"], ["memory_namespaces.slug"]),
        sa.ForeignKeyConstraint(["reminder_occurrence_id"], ["reminder_occurrences.id"]),
    )
    op.create_index(
        "notifications_namespace_id_idx", "notifications", ["namespace", "id"]
    )
    op.create_table(
        "notification_acknowledgements",
        sa.Column("notification_id", sa.BigInteger(), primary_key=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["notification_id"], ["notifications.id"], ondelete="CASCADE"
        ),
    )
    op.create_table(
        "daily_briefings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["namespace"], ["memory_namespaces.slug"]),
        sa.UniqueConstraint("namespace", "local_date", name="daily_briefing_namespace_date_uq"),
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION reject_notification_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'notifications are append-only'; END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER notifications_immutable
        BEFORE UPDATE OR DELETE ON notifications
        FOR EACH ROW EXECUTE FUNCTION reject_notification_mutation()"""
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS notifications_immutable ON notifications")
    op.execute("DROP FUNCTION IF EXISTS reject_notification_mutation")
    op.drop_table("notification_acknowledgements")
    op.drop_table("daily_briefings")
    op.drop_index("notifications_namespace_id_idx", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("reminder_occurrences_due_idx", table_name="reminder_occurrences")
    op.drop_table("reminder_occurrences")
    op.drop_index("reminders_due_idx", table_name="reminders")
    op.drop_table("reminders")
