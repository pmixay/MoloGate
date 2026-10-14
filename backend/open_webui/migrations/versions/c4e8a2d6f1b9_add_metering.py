"""Mologate: metering, account windows, readings, intervals and calibration.

Revision ID: c4e8a2d6f1b9
Revises: b5d9e1f3a7c2
"""

import sqlalchemy as sa
from alembic import op

revision = 'c4e8a2d6f1b9'
down_revision = 'b5d9e1f3a7c2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'account_window',
        sa.Column('account_id', sa.Text(), primary_key=True),
        sa.Column('window', sa.String(16), primary_key=True),
        sa.Column('used_percent', sa.Float(), nullable=False),
        sa.Column('resets_at', sa.BigInteger(), nullable=True),
        sa.Column('window_minutes', sa.Integer(), nullable=True),
        sa.Column('source', sa.String(16), nullable=False),
        sa.Column('observed_at', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'account_reading',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('account_id', sa.Text(), nullable=False),
        sa.Column('window', sa.String(16), nullable=False),
        sa.Column('used_percent', sa.Float(), nullable=False),
        sa.Column('resets_at', sa.BigInteger(), nullable=True),
        sa.Column('window_minutes', sa.Integer(), nullable=True),
        sa.Column('source', sa.String(16), nullable=False),
        sa.Column('observed_at', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_account_reading_account_window', 'account_reading', ['account_id', 'window', 'observed_at'])
    op.create_table(
        'meter_interval',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('account_id', sa.Text(), nullable=False),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('window', sa.String(16), nullable=False),
        sa.Column('start', sa.BigInteger(), nullable=False),
        sa.Column('end', sa.BigInteger(), nullable=False),
        sa.Column('used_start', sa.Float(), nullable=False),
        sa.Column('used_end', sa.Float(), nullable=False),
        sa.Column('rise', sa.Float(), nullable=False),
        sa.Column('weight', sa.Float(), nullable=False),
        sa.Column('requests', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('features', sa.Text(), nullable=True),
        sa.Column('first', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('expected', sa.Float(), nullable=True),
        sa.Column('outside', sa.Float(), nullable=True),
        sa.Column('outside_event_id', sa.Text(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_meter_interval_account_window', 'meter_interval', ['account_id', 'window', 'end'])
    op.create_index('ix_meter_interval_provider_window', 'meter_interval', ['provider', 'window', 'end'])
    op.create_table(
        'usage_calibration',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('window', sa.String(16), nullable=False),
        sa.Column('values', sa.Text(), nullable=False),
        sa.Column('samples', sa.Integer(), nullable=False),
        sa.Column('error', sa.Float(), nullable=True),
        sa.Column('fitted_at', sa.BigInteger(), nullable=False),
    )
    # Outside-use rows belong to one window (5h or weekly); request rows to none.
    op.add_column('usage_event', sa.Column('window', sa.String(16), nullable=True))


def downgrade():
    with op.batch_alter_table('usage_event') as batch:
        batch.drop_column('window')
    op.drop_table('usage_calibration')
    op.drop_table('meter_interval')
    op.drop_table('account_reading')
    op.drop_table('account_window')
