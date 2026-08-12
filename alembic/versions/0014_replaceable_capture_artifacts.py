"""allow capture evidence to be refreshed in place"""

from alembic import op


revision = "0014_capture_refresh"
down_revision = "0013_reasoning_token_accounting"
branch_labels = None
depends_on = None


def _artifact_mutation_function() -> str:
    return """
        CREATE OR REPLACE FUNCTION reject_worker_artifact_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.kind IN ('screenshot', 'browser_evidence') THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'worker artifacts are immutable';
        END;
        $$ LANGUAGE plpgsql
    """


def _immutable_artifact_function() -> str:
    return """
        CREATE OR REPLACE FUNCTION reject_worker_artifact_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'worker artifacts are immutable';
        END;
        $$ LANGUAGE plpgsql
    """


def upgrade() -> None:
    op.execute(_artifact_mutation_function())


def downgrade() -> None:
    op.execute(_immutable_artifact_function())
