"""
Utilities for handling starsystem names and the like.

This is specifically named 'starsystem' rather than 'system' for reasons that should be obvious.
"""
from time import time
import datetime
import re
import threading
from collections import OrderedDict


try:
    import collections.abc as collections_abc
except ImportError:
    import collections as collections_abc

import requests
from sqlalchemy import sql, orm
from ratlib.db import get_status, get_session, with_session, Starsystem, StarsystemPrefix
from ratlib.bloom import BloomFilter

import logging
logger = logging.getLogger(__name__)


class Style:
    """
    Defines a starsystem naming style.  Used mostly for statistical analysis currently.

    Starsystem styles are a 32-bit integer for easy representation in the database.  They have the following encoding:

    style_number = n & 0x000000ff  (253 possible styles, 0 is unknown, 255 is reserved)
    global_flags = n & 0x0000ff00 >> 8
      1: FLAG_INVALID - Most likely bad data
    data: n &0xffff >> 16
      style-dependant.
    """
    STYLES = OrderedDict()
    FLAG_INVALID = 1
    number = 0

    __slots__ = ('flags', 'data')

    def __init__(self, flags=0, data=0, value=None):
        if value is not None:
            self.value = value
        else:
            self.flags = flags
            self.data = data

    @property
    def value(self):
        return self.number | (self.flags << 8) | (self.data << 16)

    @value.setter
    def value(self, value):
        assert value & 0xff == self.number
        self.flags = (value & 0xff00) >> 8
        self.data = value >> 16

    def __int__(self):
        return self.value

    @classmethod
    def from_int(cls, value):
        number = value & 0xff
        if number not in cls.STYLES:
            raise ValueError("Unknown style type {}".format(number), number)
        return cls(value=value)

    @classmethod
    def from_name(cls, name):
        """Returns a properly configured Style if the starsystem name matches it, None otherwise"""
        return cls()

    @classmethod
    def identify(self, name):
        for style in self.STYLES.values():
            result = style.from_name(name)
            if result:
                return result
        return self.from_name(name)

    @classmethod
    def register(cls, style):
        cls.STYLES[style.number] = style
        return style

    def __bool__(self):
        return self.value != 0


@Style.register
class ProcgenStyle(Style):
    number = 1

    regex = re.compile(r'(.+) ([a-z][a-z]-[a-z] [a-z]\d+(?:-\d+)?)( .*)?')
    regex_sector = re.compile('[a-z]+ \d+ sector')

    DFLAG_IS_SECTOR = 1

    @classmethod
    def from_name(cls, name):
        result = cls.regex.fullmatch(name)
        if not result:
            return None
        prefix, _, suffix = result.groups()
        flags = cls.FLAG_INVALID if suffix else 0
        data = cls.DFLAG_IS_SECTOR if cls.regex_sector.fullmatch(prefix) else 0
        return cls(flags=flags, data=data)


@Style.register
class TrailingNumberStyle(Style):
    number = 2
    regex = re.compile(r'.* \d+')

    @classmethod
    def from_name(cls, name):
        if cls.regex.fullmatch(name):
            return cls()
        return None


@Style.register
class TwoMassStyle(Style):  # 2MassStyle is not a valid identifier
    number = 3

    regex = re.compile(r'(.+) (j\d+[-+]\d+)( .*)?')

    @classmethod
    def from_name(cls, name):
        result = cls.regex.fullmatch(name)
        if not result:
            return None
        prefix, _, suffix = result.groups()
        flags = cls.FLAG_INVALID if suffix else 0
        return cls(flags=flags)


@Style.register
class VNumberStyle(Style):
    number = 4

    regex = re.compile(r'(v\d+) ([a-z]+)( .*)?')

    @classmethod
    def from_name(cls, name):
        result = cls.regex.fullmatch(name)
        if not result:
            return None
        _, _, suffix = result.groups()
        flags = cls.FLAG_INVALID if suffix else 0
        return cls(flags=flags)


def chunkify(it, size):
    if not isinstance(it, collections_abc.Iterator):
        it = iter(it)
    go = True
    def _gen():
        nonlocal go
        try:
            remaining = size
            while remaining:
                remaining -= 1
                yield next(it)
        except StopIteration:
            go = False
            raise
    while go:
        yield _gen()


class ConcurrentOperationError(RuntimeError):
    pass


def refresh_database(bot, force=False, limit_one=True, callback=None, background=True, _lock=threading.Lock()):
    """
    Refreshes the database of starsystems.  Also rebuilds the bloom filter.
    :param bot: Bot instance
    :param force: True to force refresh regardless of age.
    :param limit_one: If True, prevents multiple concurrent refreshes.
    :param callback: Optional function that is called as soon as the system determines a refresh is needed.
        If running in the background, the function will be called immediately prior to the background task being
        scheduled.
    :param background: If True and a refresh is needed, it is submitted as a background task rather than running
        immediately.
    :param _lock: Internal lock against multiple calls.
    :returns: False if no refresh was needed.  Otherwise, a Future if background is True or True if a refresh occurred.
    :raises: ConcurrentOperationError if a refresh was already ongoing and limit_one is True.
    """
    release = False  # Whether to release the lock we did (not?) acquire.

    try:
        if limit_one:
            release = _lock.acquire(blocking=False)
            if not release:
                raise ConcurrentOperationError('refresh_database call already in progress')
        result = _refresh_database(bot, force, callback, background)
        if result and background and release:
            result.add_done_callback(lambda *a, **kw: _lock.release())
            release = False
        return result
    finally:
        if release:
            _lock.release()


@with_session
def _refresh_database(bot, force=False, callback=None, background=False, db=None):
    """
    Actual implementation of refresh_database.

    Refreshes the database of starsystems.  Also rebuilds the bloom filter.
    :param bot: Bot instance
    :param force: True to force refresh
    :param callback: Optional function that is called as soon as the system determines a refresh is needed.
    :param background: If True and a refresh is needed, it is submitted as a background task rather than running
        immediately.
    :param db: Database handle
    """
    start = time()
    status = get_status(db)

    if not (
        force or
        not status.starsystem_refreshed or
        (
            ((datetime.datetime.now(tz=datetime.timezone.utc) - status.starsystem_refreshed).total_seconds()) >
            bot.config.ratbot.edsm_maxage
        )
    ):
        return False

    if callback:
        callback()

    if background:
        return bot.data['ratbot']['executor'].submit(
            _refresh_database, bot, force=True, callback=None, background=False
        )

    fetch_start = time()
    data = requests.get(bot.config.ratbot.edsm_url).json()
    fetch_end = time()
    # with open('run/systems.json') as f:
    #     import json
    #     data = json.load(f)

    logger.debug("Clearing old data from DB.")

    db.query(Starsystem).delete()  # Wipe all old data
    db.query(StarsystemPrefix).delete()  # Wipe all old data

    # The list of prefixes isn't too terribly long, so we can just build it as we go.  This contains a set of
    # (prefix, length) tuples.
    prefixes = set()

    # Pass 1: Load JSON data into stats table.
    systems = []
    ct = 0

    logger.debug("Adding starsystems.")

    def _format_system(s):
        nonlocal ct
        ct += 1
        words = re.findall(r'\S+', s['name'])
        name = " ".join(words)
        name_lower = name.lower()
        rv = {
            'name': name,
            'name_lower': name_lower,
            'word_ct': len(words),
            'style': int(Style.identify(name_lower))
        }
        rv.update((k, s['coords'].get(k) if 'coords' in s else None) for k in 'xyz')
        prefixes.add((words[0].lower(), len(words)))
        return rv
    load_start = time()
    for chunk in chunkify(data, 5000):
        db.bulk_insert_mappings(Starsystem, [_format_system(s) for s in chunk])
        # print(ct)

    db.connection().execute("ANALYZE " + Starsystem.__tablename__)
    load_end = time()

    logger.debug("Added {} starsystems in {}".format(len(data), load_end - load_start))
    del data

    logger.debug("Adding starsystem prefixes.")

    stats_start = time()
    # Pass 2: Calculate statistics.
    # The new way of doing this should be much faster, but we lose the 'const_words' processing.  That's fine, we
    # weren't using it.  But just in case we want to use it again, it's commented out below.
    for chunk in chunkify(prefixes, 1000):
        db.bulk_insert_mappings(StarsystemPrefix, [{'first_word': prefix[0], 'word_ct': prefix[1]} for prefix in chunk])

    #
    # # 2A: Quick insert of prefixes for single-name systems
    # db.connection().execute(
    #     sql.insert(StarsystemPrefix).from_select(
    #         (StarsystemPrefix.first_word, StarsystemPrefix.word_ct),
    #         db.query(Starsystem.name_lower, Starsystem.word_ct).filter(Starsystem.word_ct == 1).distinct()
    #     )
    # )
    #
    # logger.debug("Completed fast single-word stats.")
    #
    # def _gen():
    #     for s in (
    #         db.query(Starsystem)
    #         .order_by(Starsystem.word_ct, Starsystem.name_lower)
    #         .filter(Starsystem.word_ct > 1)
    #     ):
    #         first_word, *words = s.name_lower.split(" ")
    #         yield (first_word, s.word_ct), words, s
    #
    # ct = 0
    # for chunk in chunkify(itertools.groupby(_gen(), operator.itemgetter(0)), 100):
    #     for (first_word, word_ct), group in chunk:
    #         ct += 1
    #         const_words = None
    #         for _, words, system in group:
    #             if const_words is None:
    #                 const_words = words.copy()
    #             else:
    #                 for ix, (common, word) in enumerate(zip(const_words, words)):
    #                     if const_words[ix] != words[ix]:
    #                         const_words = const_words[:ix]
    #                         break
    #         prefix = StarsystemPrefix(
    #             first_word=first_word, word_ct=word_ct, const_words=" ".join(const_words)
    #         )
    #         db.add(prefix)
    #     # print(ct)
    #     db.flush()

    logger.debug("Assigning starsystem prefixes.")
    name = Starsystem.name_lower  # Save typing, readable code
    name_matches = name.op('~')
    db.connection().execute(
        sql.update(
            Starsystem, values={
                Starsystem.prefix_id: db.query(StarsystemPrefix.id).filter(
                    StarsystemPrefix.first_word == sql.func.split_part(Starsystem.name_lower, ' ', 1),
                    StarsystemPrefix.word_ct == Starsystem.word_ct
                ).as_scalar()
            }
        )
    )

    logger.debug("Calculating statistics.")

    db.connection().execute(
        """
        UPDATE {sp} SET ratio=t.ratio, cume_ratio=t.cume_ratio
        FROM (
            SELECT t.id, ct/SUM(ct) OVER w AS ratio, SUM(ct) OVER p/SUM(ct) OVER w AS cume_ratio
            FROM (
                SELECT sp.*, COUNT(*) AS ct
                FROM
                    {sp} AS sp
                    INNER JOIN {s} AS s ON s.prefix_id=sp.id
                GROUP BY sp.id
                HAVING COUNT(*) > 0
            ) AS t
            WINDOW
            w AS (PARTITION BY t.first_word ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING),
            p AS (PARTITION BY t.first_word ORDER BY t.word_ct ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
        ) AS t
        WHERE t.id=starsystem_prefix.id
        """.format(sp=StarsystemPrefix.__tablename__, s=Starsystem.__tablename__)
    )

    logger.debug("Calcuated name length ratios.")

    stats_end = time()

    # Update refresh time
    status = get_status(db)
    status.starsystem_refreshed = sql.func.clock_timestamp()
    db.add(status)
    db.commit()

    logger.debug("Done with starsystem refresh.  Recalculating bloom filter.")

    bloom_start = time()
    refresh_bloom(bot)
    bloom_end = time()
    end = time()
    stats = {
        'stats': stats_end - stats_start, 'load': load_end - load_start, 'fetch': fetch_end - fetch_start,
        'bloom': bloom_end - bloom_start
    }
    stats['misc'] = (end - start) - sum(stats.values())
    stats['all'] = end - start
    bot.data['ratbot']['stats']['starsystem_refresh'] = stats
    return True


@with_session
def refresh_bloom(bot, db):
    """
    Refreshes the bloom filter.

    :param bot: Bot storing the bloom filter
    :param db: Database handle
    :return: New bloom filter.
    """
    # Get filter planning statistics
    count = db.query(sql.func.count(sql.distinct(StarsystemPrefix.first_word))).scalar() or 0
    bits, hashes = BloomFilter.suggest_size_and_hashes(rate=0.01, count=max(32, count), max_hashes=10)
    bloom = BloomFilter(bits, BloomFilter.extend_hashes(hashes))
    start = time()
    bloom.update(x[0] for x in db.query(StarsystemPrefix.first_word).distinct())
    end = time()
    logger.info(
        "Recomputing bloom filter took {} seconds.  {}/{} bits, {} hashes, {} false positive chance"
        .format(end-start, bloom.setbits, bloom.bits, hashes, bloom.false_positive_chance())
    )
    bot.data['ratbot']['starsystem_bloom'] = bloom
    bot.data['ratbot']['stats']['starsystem_bloom'] = {'entries': count, 'time': end - start}
    return bloom


def scan_for_systems(bot, line, min_ratio=0.05, min_length=6):
    """
    Scans for system names that might occur in the line of text.

    :param bot: Bot
    :param line: Line of text
    :param min_ratio: Minimum cumulative ratio to consider an acceptable match.
    :param min_length: Minimum length of the word matched on a single-word match.
    :return: Set of matched systems.

    min_ratio explained:

    There's one StarsystemPrefix for each distinct combination of (first word, word count).  Each prefix has a
    'ratio': the % of starsystems belonging to it as related the total number of starsystems owned by all other
    prefixes sharing the same first_word.  That is:
        count(systems with the same first word and word count) / count(systems with the same first word, any word count)

    Additionally, there's a 'cume_ratio' (Cumulative Ratio) that is: The sum of this prefix's ratio and all other
    related prefixes with a lower word count.

    min_ratio excludes prefixes with a cume_ratio below min_ratio.  The main reason for this is to exclude certain
    matches that might be made as a result of a typo -- e.g. matching a sector name rather than sector+coords because
    the coordinates were mistyped or the system in question isn't in EDSM yet.
    """
    # Split line into words.
    #
    # Rather than use a complicated regex (which we end up needing to filter anyways), we split on any combination of:
    # 0+ non-word characters, followed by 1+ spaces, followed by 0+ non-word characters.
    # This filters out grammar like periods at ends of sentences and commas between words, without filtering out things
    # like a hyphen in a system name (since there won't be a space in the right place.)
    words = list(filter(None, re.split(r'\W*\s+\W*', ' ' + line.lower() + ' ')))

    # Check for words that are in the bloom filter.  Make a note of their location in the word list.
    bloom = bot.data['ratbot']['starsystem_bloom']
    candidates = {}
    for ix, word in enumerate(words):
        if word in candidates:
            candidates[word].append(ix)
        elif word in bloom:
            candidates[word] = [ix]

    # No candidates; bail.
    if not candidates:
        return set()

    # Still here, so find prefixes in the database
    results = {}
    with get_session(bot) as db:
        # Find matching prefixes
        for prefix in db.query(StarsystemPrefix).filter(
                StarsystemPrefix.first_word.in_(candidates.keys()),
                StarsystemPrefix.cume_ratio >= min_ratio,
                sql.or_(StarsystemPrefix.word_ct > 1, sql.func.length(StarsystemPrefix.first_word) >= min_length)
        ):
            # Look through matching words.
            for ix in candidates[prefix.first_word]:
                # Bail if there's not enough room for the rest of this prefix.
                # (e.g. last word of the line was "MCC", with no room for a possible "811")
                endix = ix + prefix.word_ct
                if endix > len(words):
                    break
                # Try to find the actual system.
                check = " ".join(words[ix:endix])
                system = db.query(Starsystem).filter(Starsystem.name_lower == check).first()
                if not system or (prefix.first_word in results and len(results[prefix.first_word]) > len(system.name)):
                    continue
                results[prefix.first_word] = system.name
        return set(results.values())
