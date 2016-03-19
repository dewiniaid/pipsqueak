"""
Database support module.
"""
import functools
import multiprocessing
import logging
import sqlalchemy as sa
from sqlalchemy import orm

from .models import *

logger = logging.getLogger(__name__)


def run_alembic(filename, url):
    import alembic.command
    import alembic.config
    cfg = alembic.config.Config(filename)
    cfg.set_main_option("sqlalchemy.url", url)
    alembic.command.upgrade(cfg, "head")


def setup(bot, upgrade=True):
    """
    Initial SQLAlchemy setup for this bot session.  Also performs in-place db upgrades.

    :param bot: IRC Bot instance, used for configuration
    :param upgrade: If True (default), the database is upgrade on startup.
    """
    global engine, session_factory
    url = bot.config.ratbot.database
    if not url:
        raise ValueError("Database is not configured.")

    # Schema migration/upgrade.  Must be done in a separate process because Alembic mucks with the loggers.
    if upgrade:
        logger.debug("Checking to see if the database needs to be upgraded.")
        process = multiprocessing.Process(target=run_alembic, args=(bot.config.ratbot.alembic, url))
        process.start()
        process.join()
        if process.exitcode:
            raise RuntimeError("Alembic subprocess terminated with unexpected error {}".format(process.exitcode))
    else:
        logger.debug("Skipping database upgrade check.")
    engine = sa.create_engine(url)
    bot.data['db'] = orm.scoped_session(orm.sessionmaker(sa.create_engine(url)))

    db = get_session(bot)
    status = get_status(db)
    if status is None:
        status = Status(id=1, starsystem_refreshed=None)
        db.add(status)
        db.commit()
    db.close()


def get_session(bot):
    """
    Returns a database session.
    """
    if hasattr(bot, 'bot'):
        return get_session(bot.bot)
    return bot.data['db']()


def with_session(fn=None):
    """
    Ensures that a database session is is passed to the wrapped function as a 'db' parameter.

    :param fn: Function to wrap.

    If fn is None, returns a decorator rather than returning the decorating fn.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if 'db' in kwargs:
                return fn(*args, **kwargs)
            db = get_session(args[0])
            try:
                return fn(*args, db=db, **kwargs)
            finally:
                db.close()
        return wrapper
    return decorator(fn) if fn else decorator


def get_status(db):
    return db.query(Status).get(1)
