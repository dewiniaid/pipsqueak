"""Adds rudimentary privileges and accounts system.

Revision ID: 1e36614494a2
Revises: 4a321610d403
Create Date: 2016-03-17 19:38:25.617128

"""

# revision identifiers, used by Alembic.
revision = '1e36614494a2'
down_revision = '4a321610d403'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa
import itertools


def upgrade():
    # Lots of foreign keys, so set up a little macro
    import functools
    fk = functools.partial(sa.ForeignKey, ondelete='cascade', onupdate='cascade')

    # Privilege definitions
    table_priv = op.create_table(
        'priv',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('name', sa.Text, unique=True, nullable=False)
    )
    # Privilege inherits
    table_priv_inherits = op.create_table(
        'priv_inherits',
        sa.Column('parent_id', sa.Integer, fk('priv.id'), nullable=False),
        sa.Column('child_id', sa.Integer, fk('priv.id'), nullable=False),
        sa.PrimaryKeyConstraint('parent_id', 'child_id')
    )
    # Initial privileges
    privs = {
        'shutdown': {'shutdown.shutdown', 'shutdown.restart'},
        'channel': {'channel.join', 'channel.part'},
        'fact': {'fact.edit', 'fact.import', 'fact.full'},
        'fact.edit': {'fact.add', 'fact.del'},
        'sysrefresh.force': {'sysrefresh'},
        'auth': {'auth.grant', 'auth.revoke', 'auth.groups'},
        '*': None
    }
    all_privs = set()
    for k, v in privs.items():
        all_privs.add(k)
        if v:
            all_privs.update(v)
    all_privs = dict(row for row in op.get_bind().execute(
        table_priv.insert(list({'name': name} for name in all_privs))
        .returning(table_priv.c.name, table_priv.c.id)
    ))

    data = []
    for parent, parent_id in all_privs.items():
        children = privs.get(parent)
        if not children:
            continue
        data.extend({'parent_id': parent_id, 'child_id': all_privs[child]} for child in children)
    if data:
        op.bulk_insert(table_priv_inherits, data)

    # Account
    op.create_table(
        'account',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('name', sa.Text, nullable=False, unique=True),  # IRC account name
        sa.Column('iam_platform', sa.Text, nullable=True)  # Default platform for this rat when using IAM.
    )

    # Account privilege
    op.create_table(
        'account_priv',
        sa.Column('account_id', sa.Integer, fk('account.id'), nullable=False, primary_key=True),
        sa.Column('priv_id', sa.Integer, fk('priv.id'), nullable=False, primary_key=True)
    )

    # Account active rat
    op.create_table(
        'account_iam',
        sa.Column('account_id', sa.Integer, fk('account.id'), nullable=False, primary_key=True),
        sa.Column('platform', sa.Text, nullable=False, primary_key=True),
        sa.Column('name', sa.Text, nullable=False),
        sa.Column('api_id', sa.Text, nullable=True)
    )

def downgrade():
    for table in ('priv_inherits', 'account_priv', 'priv', 'account_iam', 'account'):
        op.drop_table(table)

    pass
