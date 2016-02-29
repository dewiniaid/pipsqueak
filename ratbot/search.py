#coding: utf8
"""
search.py - Elite Dangerous System Search module.
Copyright 2015, Dimitri "Tyrope" Molenaars <tyrope@tyrope.nl>
Licensed under the Eiffel Forum License 2.

This module is built on top of the Sopel system.
http://sopel.chat/
"""

#Python core imports
import json
import os
import datetime

#Sopel imports
from sopel.module import commands, interval, example, NOLIMIT, HALFOP, OP
from sopel.tools import SopelMemory

from sqlalchemy import sql, orm

import ratlib
import ratlib.sopel
from ratlib import friendly_timedelta
from ratlib.db import with_session, Starsystem, StarsystemPrefix, get_status
from ratlib.starsystem import refresh_database, scan_for_systems, ConcurrentOperationError
from ratlib.autocorrect import correct
import re
from ratbot import *
from ircbot.commands import UsageError


def setup(bot):
    frequency = int(bot.config.ratbot.edsm_autorefresh or 0)
    if frequency > 0:
        bot.eventloop.schedule_periodically(frequency, task_sysrefresh, bot)

@command('search')
@bind('<system:line>', summary='Searches for similarly-named starsystems.')
@with_session
def search(event, db, system):
    """
    Searches for system name matches.
    """
    if system:
        system = re.sub(r'\s\s+', ' ', system.strip())
    if not system:
        raise UsageError()

    if len(system) > 100:
        # Postgres has a hard limit of 255, but this is long enough.
        event.reply("System name is too long.")

    # Try autocorrection first.
    result = correct(system)
    if result.fixed:
        system = result.output
    system_name = '"{}"'.format(system)
    if result.fixed:
        system_name += " (autocorrected)"
    system = system.lower()

    # Levenshtein expression
    max_distance = 10
    max_results = 4
    expr = sql.func.levenshtein_less_equal(Starsystem.name_lower, system, max_distance)

    # Query
    result = (
        db.query(Starsystem, expr.label("distance"))
        .filter(expr <= max_distance)
        .order_by(expr.asc())
    )[:max_results]

    if result:
        return event.reply("Nearest matches for {system_name} are: {matches}".format(
            system_name=system_name,
            matches=", ".join('"{0.Starsystem.name}" [{0.distance}]'.format(row) for row in result)
        ))
    return event.reply("No similar results for {system_name}".format(system_name=system_name))


def refresh_time_stats(bot):
    """
    Returns formatted stats on the last refresh.
    """
    stats = bot.data['ratbot']['stats'].get('starsystem_refresh')
    if not stats:
        return "No starsystem refresh stats are available."
    return (
        "Refresh took {all:.2f} seconds.  (Fetch: {fetch:.2f}; Load: {load:.2f}; Stats: {stats:.2f}; Misc: {misc:.2f})"
        .format(**stats)
    )


@command('sysstats')
@bind('what=count/bloom/refresh/all')
@with_session
def cmd_sysstats(event, what='count', db=None):
    """Diagnostics and statistics."""
    def ct(table, *filters):
        result = db.query(sql.func.count()).select_from(table)
        if filters:
            result = result.filter(*filters)
        return result.scalar()

    stats = event.bot.data['ratbot']['stats']

    what = what.lower()
    all_options = {'count', 'bloom', 'refresh', 'all'}
    options = (set((what or '').lower().split(' ')) & all_options) or {'count'}

    if 'all' in options:
        options = all_options

    if 'count' in options:
        count_stats = {
            'excluded': (
                db.query(sql.func.count(Starsystem.id))
                .join(StarsystemPrefix, StarsystemPrefix.id == Starsystem.prefix_id)
                .filter(sql.or_(
                    StarsystemPrefix.cume_ratio < 0.05,
                    sql.and_(StarsystemPrefix.word_ct <= 1, sql.func.length(StarsystemPrefix.first_word) < 6)
                ))
                .scalar()
            ),
            'count': ct(Starsystem),
            'prefixes': ct(StarsystemPrefix),
            'one_word': ct(StarsystemPrefix, StarsystemPrefix.word_ct == 1)
        }
        count_stats['pct'] = 0 if not count_stats['count'] else count_stats['excluded'] / count_stats['count']

        event.reply(
            "{count} starsystems under {prefixes} unique prefixes."
            " {one_word} single word systems. {excluded} ({pct:.0%}) systems excluded from system name detection."
            .format(**count_stats)
        )

    if 'refresh' in options:
        event.reply(refresh_time_stats(event.bot))

    if 'bloom' in options:
        bloom_stats = stats.get('starsystem_bloom')
        bloom = event.bot.data['ratbot']['starsystem_bloom']

        if not bloom_stats or not bloom:
            event.reply("Bloom filter stats are unavailable.")
        else:
            event.reply(
                "Bloom filter generated in {time:.2f} seconds. k={k}, m={m}, n={entries}, {numset} bits set,"
                " {pct:.2%} false positive chance."
                .format(k=bloom.k, m=bloom.m, pct=bloom.false_positive_chance(), numset=bloom.setbits, **bloom_stats)
            )

def task_sysrefresh(bot):
    try:
        refresh_database(bot, background=True, callback=lambda: print("Starting background EDSM refresh."))
    except ConcurrentOperationError:
        pass


@command('sysrefresh')
@bind('[force=-f]', summary='Refresh the starsystme list (-f: force)')
@with_session
def cmd_sysrefresh(event, force=False, db=None):
    """
    Refreshes the starsystem database if you have halfop or better.  Reports the last refresh time otherwise.

    -f: Force refresh even if data is stale.  Requires op.
    """
    # TODO: Reimplement access checking
    privileged = True
    # access = ratlib.sopel.best_channel_mode(bot, trigger.nick)
    # privileged = access & (HALFOP | OP)
    msg = ""

    if privileged:
        try:
            refreshed = refresh_database(
                event, force=bool(force), callback=lambda: event.reply("Starting starsystem refresh...")
            )
            if refreshed:
                event.reply(refresh_time_stats(event.bot))
                return
            msg = "Not yet.  "
        except ConcurrentOperationError:
            event.reply("A starsystem refresh operation is already in progress.")
            return

    when = get_status(db).starsystem_refreshed
    if not when:
        msg += "The starsystem database appears to have never been initialized."
    else:
        when = when.astimezone(datetime.timezone.utc)
        msg += "The starsystem database was refreshed at {} ({})".format(
            ratlib.format_timestamp(when), ratlib.format_timedelta(when)
        )
    event.reply(msg)


@command('scan')
@bind('<text:line>')
def cmd_scan(event, trigger, text):
    """
    Used for system name detection testing.
    """
    if text:
        text = text.strip()
    if not text:
        raise UsageError()
    results = scan_for_systems(event, text)
    event.reply("Scan results: {}".format(", ".join(results) if results else "no match found"))
