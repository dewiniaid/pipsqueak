"""Starsystem data changes

Revision ID: 4a321610d403
Revises: aad7f336662f
Create Date: 2016-03-11 11:17:52.435474

"""

# revision identifiers, used by Alembic.
revision = '4a321610d403'
down_revision = 'aad7f336662f'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('starsystem', sa.Column('style', sa.Integer()))
    op.drop_column('starsystem_prefix', 'const_words')
    op.execute("UPDATE status SET starsystem_refreshed = NULL")


def downgrade():
    pass
