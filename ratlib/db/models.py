import sqlalchemy as sa
from sqlalchemy import sql, orm, schema
from sqlalchemy.orm.collections import InstrumentedSet
from sqlalchemy.ext.declarative import as_declarative, declared_attr
from sqlalchemy.ext.associationproxy import association_proxy
import re


__all__ = [
    'ModelBase', 'Fact', 'Status', 'StarsystemPrefix', 'Starsystem',
    'PrivInherits', 'Priv', 'AccountPriv', 'Account', 'AccountIAm',
]

@as_declarative(metadata=schema.MetaData())
class ModelBase:
    """
    Base class for tables.
    """
    __abstract__ = True

    # noinspection PyMethodParameters
    @declared_attr
    def __tablename__(cls):
        """
        Generate table names by replacing every occurrence of e.g. "aA" with "a_a".  This effectively converts
        camelCase and TitleCase to underscore_separated.

        Also prefixes all names with
        """
        return re.sub(r'([^A-Z])([A-Z])', r'\1_\2', cls.__name__).lower()


def _listify(x):
    if not x:
        return []
    if isinstance(x, str):
        x = [x.strip().lower()]
    return list(i.strip().lower() for i in x)


class Fact(ModelBase):
    name = sa.Column(sa.Text, primary_key=True)
    lang = sa.Column(sa.Text, primary_key=True)
    message = sa.Column(sa.Text, nullable=False)
    author = sa.Column(sa.Text, nullable=True)

    def __init__(self, name=None, lang=None, message=None, author=None):
        if name:
            name = name.lower().strip() or None
        if lang:
            lang = lang.lower().strip() or None
        # noinspection PyArgumentList
        super().__init__(name=name, lang=lang, message=message, author=author)

    @classmethod
    def query(cls, db, name=None, lang=None, order_by=True):
        name = _listify(name)
        lang = _listify(lang)

        query = db.query(cls)
        if len(name) == 1:
            query = query.filter(cls.name == name[0])
        elif len(name) > 1:
            query = query.filter(cls.name.in_(name))
        if len(lang) == 1:
            query = query.filter(cls.lang == lang[0])
        elif len(lang) > 1:
            query = query.filter(cls.lang.in_(lang))

        # Handle ordering
        if order_by:
            if order_by is True:
                if not lang:
                    query = query.order_by(cls.lang)
                elif len(lang) > 1:
                    query = query.order_by(
                        sql.case(
                            value=cls.lang,
                            whens=list((item, ix) for ix, item in enumerate(lang))
                        )
                    )
                if len(name) != 1:
                    query = query.order_by(cls.name)
            else:
                # noinspection PyArgumentList
                query = query.order_by(*order_by)
        return query

    @classmethod
    def find(cls, db, name=None, lang=None, order_by=True):
        return cls.query(db, name, lang, order_by).first()

    @classmethod
    def findall(cls, db, name=None, lang=None, order_by=True):
        yield from cls.query(db, name, lang, order_by)

    @classmethod
    def unique_query(cls, db, name=None, lang=None, field=None, order_by=True):
        if order_by:
            order_by = [field]
        return (
            cls.query(db, name, lang, order_by)
            .with_entities(field)
            .distinct()
        )

    @classmethod
    def unique_names(cls, db, name=None, lang=None, order_by=True):
        for item in cls.unique_query(db, name, lang, cls.name, order_by):
            yield item[0]

    @classmethod
    def unique_langs(cls, db, name=None, lang=None, order_by=True):
        for item in cls.unique_query(db, name, lang, cls.lang, order_by):
            yield item[0]


class Status(ModelBase):
    id = sa.Column(sa.Integer, primary_key=True)
    starsystem_refreshed = sa.Column(sa.DateTime(timezone=True), nullable=True)  # Time of last refresh


class StarsystemPrefix(ModelBase):
    id = sa.Column(sa.Integer, primary_key=True)
    first_word = sa.Column(sa.Text, nullable=False)
    word_ct = sa.Column(sa.Integer, nullable=False)
    # const_words = sa.Column(sa.Text, nullable=True)
    ratio = sa.Column('ratio', sa.Float())
    cume_ratio = sa.Column('cume_ratio', sa.Float())
StarsystemPrefix.__table__.append_constraint(schema.Index(
    'starsystem_prefix__unique_words', 'first_word', 'word_ct', unique=True
))


class Starsystem(ModelBase):
    id = sa.Column(sa.Integer, primary_key=True)
    name_lower = sa.Column(sa.Text, nullable=False)
    name = sa.Column(sa.Text, nullable=False)
    word_ct = sa.Column(sa.Integer, nullable=False)
    x = sa.Column(sa.Float, nullable=True)
    y = sa.Column(sa.Float, nullable=True)
    z = sa.Column(sa.Float, nullable=True)
    prefix_id = sa.Column(
        sa.Integer,
        sa.ForeignKey(StarsystemPrefix.id, onupdate='cascade', ondelete='set null'), nullable=True
    )
    style = sa.Column(sa.Integer, nullable=True)  # Arbitrary style identifier, see starsystem.py
    prefix = orm.relationship(StarsystemPrefix, backref=orm.backref('systems', lazy=True), lazy=True)
Starsystem.__table__.append_constraint(schema.Index('starsystem__prefix_id', 'prefix_id'))
Starsystem.__table__.append_constraint(schema.Index('starsystem__name_lower', 'name_lower'))


class PrivInherits(ModelBase):
    parent_id = sa.Column(
        'parent_id', sa.Integer, sa.ForeignKey('priv.id', onupdate='cascade', ondelete='cascade'), primary_key=True
    )
    child_id = sa.Column(
        sa.Integer, sa.ForeignKey('priv.id', onupdate='cascade', ondelete='cascade'), primary_key=True
    )
    parent = orm.relationship(
        "Priv", foreign_keys=[parent_id], backref=orm.backref('inherits', viewonly=True), viewonly=True
    )
    child = orm.relationship(
        "Priv", foreign_keys=[child_id], backref=orm.backref('inherited_by', viewonly=True), viewonly=True
    )


_t = PrivInherits.__table__


class Priv(ModelBase):
    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    name = sa.Column(sa.Text, nullable=False, unique=True)
    children = orm.relationship(
        "Priv", secondary=_t, primaryjoin=(id == _t.c.parent_id), secondaryjoin=(_t.c.child_id == id),
        collection_class=InstrumentedSet, backref=orm.backref('parents', collection_class=InstrumentedSet)
    )
    parent_names = association_proxy('parents', 'name')
    child_names = association_proxy('children', 'name')
del _t


class AccountPriv(ModelBase):
    account_id = sa.Column(
        sa.Integer, sa.ForeignKey('account.id', onupdate='cascade', ondelete='cascade'),
        nullable=False, primary_key=True
    )
    priv_id = sa.Column(
        sa.Integer, sa.ForeignKey('priv.id', onupdate='cascade', ondelete='cascade'),
        nullable=False, primary_key=True
    )
    account = orm.relationship(lambda: Account, viewonly=True, backref=orm.backref("privlinks", viewonly=True))
    priv = orm.relationship(lambda: Priv, viewonly=True, backref=orm.backref("accountlinks", viewonly=True))


class Account(ModelBase):
    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    name = sa.Column(sa.Text, nullable=False, unique=True)
    iam_platform = sa.Column(sa.Text, nullable=True)
    privs = orm.relationship(
        "Priv", secondary=AccountPriv.__table__, collection_class=InstrumentedSet,
        backref=orm.backref('accounts', collection_class=InstrumentedSet)
    )
    priv_names = association_proxy('privs', 'name')



class AccountIAm(ModelBase):
    account_id = sa.Column(
        'account_id', sa.Integer, sa.ForeignKey('account.id', onupdate='cascade', ondelete='cascade'), primary_key=True
    )
    platform = sa.Column('platform', sa.Text, nullable=False, primary_key=True)
    name = sa.Column('name', sa.Text, nullable=False)
    api_id = sa.Column('api_id', sa.Text, nullable=True)


