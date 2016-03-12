"""
board.py - Fuel Rats Cases module.

Copyright 2015, Dimitri "Tyrope" Molenaars <tyrope@tyrope.nl>
Licensed under the Eiffel Forum License 2.

This module is built on top of the Sopel system.
http://sopel.chat/
"""

import datetime
import collections
import itertools
import warnings
import contextlib
import logging
import threading
import operator
import re
import functools

import ircbot
import ircbot.commands
from ircbot.commands.exc import *

from ratbot import *
from ratbot.facts import find_fact
from ratlib.db import with_session
from ratlib import friendly_timedelta, format_timestamp
from ratlib.autocorrect import correct
from ratlib.starsystem import scan_for_systems
from ratlib.api.props import *
import ratlib.api.http
import ratlib.db

urljoin = ratlib.api.http.urljoin

bold = lambda x: x  # PLACEHOLDER  FIXME

logger = logging.getLogger(__name__)

command = functools.partial(command, category='RESCUES')


@ircbot.commands.register_type('rescue')
@ircbot.commands.register_type('fullrescue', fullresult=True)
class RescueParamType(ircbot.commands.ParamType):
    wrap_exceptions = False
    def __init__(self, param, fullresult=False):
        """
        Handles finding/creating a rescue.

        :param param: Parameter we are bound to.
        """
        super().__init__(param)
        if param.options:
            options = set(re.findall(r'[^\s,]+', param.options.lower()))
        else:
            options = set()
        self.fullresult = fullresult
        self.create = 'create' in options
        self.must_exist = 'exists' in options

    def parse(self, event, value):
        board = event.bot.data['ratboard']['board']
        result = board.find(value, create=self.create)
        if result.rescue is None:
            if self.create:
                raise UsageError("Case {} was not found and could not be created.".format(value), final=True)
            elif self.must_exist:
                raise UsageError("Case {} was not found.".format(value), final=True)
        return result if self.fullresult else result.rescue


# noinspection PyAttributeOutsideInit
class RatboardConfig(ircbot.ConfigSection):
    def read(self, section):
        self.signal = re.compile(section.get('signal', 'ratsignal'), re.IGNORECASE)
        self.case_pool_size = section.getint('case_pool_size', 10)
        self.case_history = section.getint('case_history', 10)
        self.client_history = section.getint('client_history', 5000)
bot.config.section('ratboard', RatboardConfig)


@prepare
def setup(bot):
    bot.data['ratboard'] = {
        'history': collections.OrderedDict(),
        'board': RescueBoard(maxpool=bot.config.ratboard.case_pool_size),
    }

    try:
        refresh_cases(bot)
    except ratlib.api.http.BadResponseError as ex:
        import traceback
        logger.warning("Failed to perform initial sync against the API.  Bad Things might happen.")
        logger.error(traceback.format_exc())


def callapi(bot, method, uri, data=None, _fn=ratlib.api.http.call):  # FIXME
    uri = urljoin(bot.config.ratbot.apiurl, uri)
    return _fn(method, uri, data)


FindRescueResult = collections.namedtuple('FindRescueResult', ['rescue', 'created'])


class RescueBoard:
    """
    Manages all attached cases, including API calls.
    """
    INDEX_TYPES = {
        'boardindex': operator.attrgetter('boardindex'),
        'id': operator.attrgetter('id'),
        'clientnick': lambda x: None if not x.client or not x.client['nickname'] else x.client['nickname'].lower(),
        'clientcmdr': lambda x: None if not x.client or not x.client['CMDRname'] else x.client['CMDRname'].lower(),
    }

    MAX_POOLED_CASES = 10

    def __init__(self, maxpool=None):
        self._lock = threading.RLock()
        self.indexes = {k: {} for k in self.INDEX_TYPES.keys()}

        if maxpool is None:
            maxpool = self.MAX_POOLED_CASES

        # Boardindex pool
        self.maxpool = maxpool
        self.counter = itertools.count(start=self.maxpool)
        self.pool = collections.deque(range(0, self.maxpool))

    def __enter__(self):
        return self._lock.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._lock.__exit__(exc_type, exc_val, exc_tb)

    def add(self, rescue):
        """
        Adds the selected case to our indexes.
        """
        with self:
            assert rescue.board is None, "Rescue is already assigned."
            assert rescue.boardindex is None, "Rescue already has a boardindex."
            # Assign an boardindex
            rescue.board = self
            try:
                rescue.boardindex = self.pool.popleft()
            except IndexError:
                rescue.boardindex = next(self.counter)

            # Add to indexes
            for index, fn in self.INDEX_TYPES.items():
                # FIXME: This will fail horribly if the function raises
                key = fn(rescue)
                if key is None:
                    continue
                if key in self.indexes[index]:
                    warnings.warn("Key {key!r} is already in index {index!r}".format(key=key, index=index))
                    continue
                self.indexes[index][key] = rescue

    def remove(self, rescue):
        """
        Removes the selected case from our indexes.
        """
        with self:
            # Remove from indexes
            assert rescue.board is self, "Rescue is not ours."
            assert rescue.boardindex is not None, "Rescue had no boardindex."
            for index, fn in self.INDEX_TYPES.items():
                key = fn(rescue)
                if key is None:
                    continue
                if self.indexes[index].get(key) != rescue:
                    msg = "Key {key!r} in index {index!r} does not belong to this rescue.".format(key=key, index=index)
                    warnings.warn(msg)
                    logger.warning(msg)
                    continue
                del self.indexes[index][key]

            # Reclaim numbers
            if rescue.boardindex < self.maxpool:
                self.pool.append(rescue.boardindex)
            if not self.indexes['boardindex']:  # Board is clear.
                self.counter = itertools.count(start=self.maxpool)

    @contextlib.contextmanager
    def change(self, rescue):
        """
        Returns a context manager that snapshots case attributes and updates the indexes with any relevant changes.

        Usage Example:
        ```
        with board.change(rescue):
            rescue.client['CMDRname'] = cmdrname
        """
        with self:
            assert rescue.board is self
            snapshot = dict({index: fn(rescue) for index, fn in self.INDEX_TYPES.items()})
            yield rescue
            assert rescue.board is self  # In case it was changed
            for index, fn in self.INDEX_TYPES.items():
                new = fn(rescue)
                old = snapshot[index]
                if old != new:
                    if old is not None:
                        if self.indexes[index].get(old) != rescue:
                            msg = (
                                "Key {key!r} in index {index!r} does not belong to this rescue."
                                .format(key=old, index=index)
                            )
                            warnings.warn(msg)
                            logger.warning(msg)
                        else:
                            del self.indexes[index][old]
                    if new is not None:
                        if new in self.indexes[index]:
                            msg = (
                                "Key {key!r} in index {index!r} does not belong to this rescue."
                                .format(key=new, index=index)
                            )
                            warnings.warn(msg)
                            logger.warning(msg)
                        else:
                            self.indexes[index][new] = rescue

    def create(self):
        """
        Creates a rescue attached to this board.
        """
        rescue = Rescue()
        self.add(rescue)
        return rescue

    def find(self, search, create=False):
        """
        Attempts to find a rescue attached to this board.  If it fails, possibly creates one instead.

        :param create: Whether to create a case that's not found.  Even if True, this only applies for certain types of
        searches.
        :return: A FindRescueResult tuple of (rescue, created), both of which will be None if no case was found.

        If `int(search)` does not raise, `search` is treated as a boardindex.  This will never create a case.

        Otherwise, if `search` begins with `"@"`, it is treated as an ID from the API.  This will never create a case.

        Otherwise, `search` is treated as a client nickname or a commander name (in that order).  If this still does
        not have a result, a new case is returned (if `create` is True).
        """
        try:
            if search and isinstance(search, str) and search[0] == '#':
                index = int(search[1:])
            else:
                index = int(search)
        except ValueError:
            pass
        else:
            rescue = self.indexes['boardindex'].get(index, None)
            return FindRescueResult(rescue, False if rescue else None)

        if not search:
            return FindRescueResult(None, None)

        if search[0] == '@':
            rescue = self.indexes['id'].get(search[1:], None),
            return FindRescueResult(rescue, False if rescue else None)

        rescue = self.indexes['clientnick'].get(search.lower()) or self.indexes['clientcmdr'].get(search.lower())
        if rescue or not create:
            return FindRescueResult(rescue, False if rescue else None)

        rescue = Rescue()
        rescue.client['CMDRname'] = search
        rescue.client['nickname'] = search
        self.add(rescue)
        return FindRescueResult(rescue, True)

    @property
    def rescues(self):
        """
        Read-only convenience property to list all known rescues.
        """
        return self.indexes['boardindex'].values()


class Rescue(TrackedBase):
    active = TrackedProperty(default=True)
    createdAt = DateTimeProperty(readonly=True)
    lastModified = DateTimeProperty(readonly=True)
    id = TrackedProperty(remote_name='_id', readonly=True)
    rats = SetProperty(default=lambda: set())
    unidentifiedRats = SetProperty(default=lambda: set())
    quotes = ListProperty(default=lambda: [])
    platform = TrackedProperty(default='unknown')
    open = TypeCoercedProperty(default=True, coerce=bool)
    epic = TypeCoercedProperty(default=False, coerce=bool)
    codeRed = TypeCoercedProperty(default=False, coerce=bool)
    client = DictProperty(default=lambda: {})
    system = TrackedProperty(default=None)
    successful = TypeCoercedProperty(default=None, coerce=bool)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.boardindex = None
        self.board = None

    def change(self):
        """
        Convenience shortcut for performing safe attribute changes (that also update indexes).

        ```
        with rescue.change():
            rescue.client['CMDRname'] = 'Foo'
        ```

        If the rescue is not attached to the board, this returns a dummy context manager that does nothing.
        """
        if self.board:
            return self.board.change(self)

        @contextlib.contextmanager
        def _dummy():
            yield self
        return _dummy()

    def refresh(self, json, merge=True):
        for prop in self._props:
            if isinstance(prop, InstrumentedProperty):
                prop.read(self, json, merge=merge)
                continue
            if merge and prop in self._changed:
                continue  # Ignore incoming data that conflicts with our pending changes.
            prop.read(self, json)

    @classmethod
    def load(cls, json, inst=None):
        """
        Creates a case from a JSON dict.
        """
        if inst is None:
            inst = cls()
        inst.refresh(json)
        return inst

    def save(self, full=False, props=None):
        result = {}
        props = self._props if full else self._changed
        for prop in props:
            prop.write(self, result)
        return result

    @property
    def client_name(self):
        """Returns the first logical name for a client."""
        t = self.client.get('nickname')
        if t:
            return t
        t = self.client.get('CMDRname')
        if t:
            return "CMDR " + t
        return "<unknown client>"

    @property
    def client_names(self):
        """Returns all known names for a client."""
        nickname = self.client.get('nickname')
        cmdrname = self.client.get('CMDRname')
        if nickname:
            if cmdrname and nickname.lower() != cmdrname.lower():
                return "{} (CMDR {})".format(nickname, cmdrname)
            return nickname
        elif cmdrname:
            return "CMDR {}".format(cmdrname)
        else:
            return "<unknown client>"

    def touch(self, when=None):
        """
        Updates modification (and potentially creation time) of this case.  Should only be used when API-less
        :param when: Time to set.  Should be a UTC timestamp
        """
        if not when:
            when = datetime.datetime.now(tz=datetime.timezone.utc)
        if not self.createdAt:
            self.createdAt = when
        self.lastModified = when
        return when


def refresh_cases(bot, rescue=None):
    """
    Grab all open cases from the API so we can work with them.
    :param bot: IRC bot
    :param rescue: Individual rescue to refresh.
    """
    if not bot.config.ratbot.apiurl:
        warnings.warn("No API URL configured.  Operating in offline mode.")
        return  # API disabled.
    uri = '/api/search/rescues'
    if rescue is not None:
        if rescue.id is None:
            raise ValueError('Cannot refresh a non-persistent case.')
        uri += "/" + rescue.id
        data = {}
    else:
        data = {'open': True}

    # Exceptions here are the responsibility of the caller.
    result = callapi(bot, 'GET', uri, data=data)  # FIXME
    board = bot.data['ratboard']['board']

    if rescue:
        if not result['data']:
            board.remove(rescue)
        else:
            with rescue.change():
                rescue.refresh(result['data'])
        return

    with board:
        # Cases we have but the refresh doesn't.  We'll assume these are closed after winnowing down the list.
        missing = set(board.indexes['id'].keys())
        for case in result['data']:
            id = case['_id']
            missing.discard(id)  # Case still exists.
            existing = board.indexes['id'].get(id)

            if existing:
                with existing.change():
                    existing.refresh(case)
                continue
            board.add(Rescue.load(case))

        for id in missing:
            case = board.indexes['id'].get(id)
            if case:
                board.remove(case)


class AppendQuotesResult:
    """
    Result information from append_quotes
    """
    def __init__(self, rescue=None, created=False,
                 added_lines=None, autocorrected=False, detected_platform=None, detected_system=None
                 ):
        """
        Creates a new AppendQuotesResult

        :param rescue: The rescue that was found/created, or None if no such rescue.
        :param created: True if the rescue was freshly created.
        :param added_lines: Lines that were added to the new case after any relevant transformations.
        :param autocorrected: True if system name autocorrection triggered.
        :param detected_platform: Set to the detected platform, or False if no platform was detected.
        :param detected_system: Set to the detected system, or False if no system was detected.
        """
        self.rescue = rescue
        self.created = created
        self.added_lines = added_lines or []
        self.autocorrected = autocorrected
        self.detected_platform = detected_platform
        self.detected_system = detected_system

    def __bool__(self):
        return self.rescue is not None

    def tags(self):
        """Convenience method."""
        if not self:
            return []
        rv = ["Case " + str(self.rescue.boardindex)]
        if self.detected_platform:
            rv.append(self.detected_platform.upper())
        if self.detected_system:
            rv.append(self.detected_system)
        if self.autocorrected:
            rv.append("Autocorrected")
        return rv


def append_quotes(bot, search, lines, autocorrect=True, create=True, detect_platform=True, detect_system=True):
    """
    Appends lines to a (possibly newly created) case.  Returns a tuple of (Rescue, appended_lines).

    If autocorrect is True, performs system autocorrection first.  In this case, appended_lines may not match the input.
    :param bot: IRC bot handle.
    :param search: Client name, case ID, boardindex, a Rescue object, or a FindRescueResult.
    :param lines: Line(s) to append.  If this is a string it is coerced to a list of strings.
    :param autocorrect: Whether to perform system autocorrection.
    :param create: Whether this is allowed to create a new case.  Passed to `Board.find()`
    :param detect_platform: If True, attempts to parse a platform out of the first line.
    :param detect_system: If True, attempts system name autodetection.
    :return: A AppendQuotesResult representing the actions that happened.
    """
    rv = AppendQuotesResult()
    if isinstance(search, Rescue):
        rv.rescue = search
        rv.created = False
    elif isinstance(search, FindRescueResult):
        rv.rescue = search.rescue
        rv.created = search.created
    else:
        rv.rescue, rv.created = bot.data['ratboard']['board'].find(search, create=create)
    if not rv:
        return rv

    if isinstance(lines, str):
        lines = [lines]
    if autocorrect:
        rv.added_lines = []
        for line in lines:
            result = correct(line)
            rv.added_lines.append(result.output)
            if result.fixed:
                rv.autocorrected = True
                originals = ", ".join('"...{name}"'.format(name=system) for system in result.corrections)
                if result.fixed > 1:
                    rv.added_lines.append("[Autocorrected system names, originals were {}]".format(originals))
                else:
                    rv.added_lines.append("[Autocorrected system name, original was {}]".format(originals))
    else:
        rv.added_lines = lines
    if rv.added_lines and detect_system and not rv.rescue.system:
        systems = scan_for_systems(bot, rv.added_lines[0])
        if len(systems) == 1:
            rv.detected_system = systems.pop()
            rv.added_lines.append("[Autodetected system: {}]".format(rv.detected_system))
            rv.rescue.system = rv.detected_system
    if detect_platform and rv.rescue.platform == 'unknown':
        platforms = set()
        for line in rv.added_lines:
            if re.search(
                r"""
                (?:[^\w-]|\A)  # Beginning of line, or non-hyphen word boundary
                pc             # ... followed by "PC"
                (?:[^\w-]|\Z)  # End of line, or non-hyphen word boundary
                """, line, flags=re.IGNORECASE | re.VERBOSE
            ):
                platforms.add('pc')

            if re.search(
                r"""
                (?:[^\w-]|\A)  # Beginning of line, or non-hyphen word boundary
                xb(?:ox)?      # ... followed by "XB" or "XBOX"
                (?:-?(?:1|one))?  # ... maybe followed by 1/one, possibly w/ leading hyphen
                (?:[^\w-]|\Z)  # End of line, or non-hyphen word boundary
                """, line, flags=re.IGNORECASE | re.VERBOSE
            ):
                platforms.add('xb')
        if len(platforms) == 1:
            rv.rescue.platform = platforms.pop()
            rv.detected_platform = rv.rescue.platform

    rv.rescue.quotes.extend(rv.added_lines)
    return rv


@rule(lambda x: True, priority=1000)  # High priority so this executes AFTER any commands.
def rule_history(event):
    """Remember the last thing somebody said."""
    # if trigger.group().startswith("\x01ACTION"): # /me
    #     line = trigger.group()[:-1]
    # else:
    #     line = trigger.group()
    key = event.nick.lower()
    history = event.bot.data['ratboard']['history']
    history[key] = (event.nick, event.message)
    history.move_to_end(key)
    while len(history) > bot.config.ratboard.client_history:
        history.popitem(False)


@rule(bot.config.ratboard.signal, attr='match', priority=-1000)
def rule_ratsignal(event):
    """Light the rat signal, somebody needs fuel."""
    if event.channel is None:
        event.qsay("In private messages, nobody can hear you scream.  (HINT: Maybe you should try this in #FuelRats)")
        return

    result = append_quotes(bot, event.nick, [event.message], create=True)
    event.say(
        "Received RATSIGNAL from {nick}.  Calling all available rats!  ({tags})"
        .format(nick=event.nick, tags=", ".join(result.tags()) if result else "<unknown>")
    )
    event.reply('Are you on emergency oxygen? (Blue timer on the right of the front view)')
    # FIXME
    # save_case_later(
    #     bot, result.rescue,
    #     "API is still not done with ratsignal from {nick}; continuing in background.".format(nick=event.nick)
    # )


@command('quote')
@bind('<rescue:rescue:exists>', "Reports all known information on the specified rescue.")
def cmd_quote(event, rescue):
    """
    Recites all known information for the specified rescue
    Required parameters: client name or case number.
    """
    tags = ['unknown platform' if not rescue.platform or rescue.platform == 'unknown' else rescue.platform.upper()]

    if rescue.epic:
        tags.append("epic")
    if rescue.codeRed:
        tags.append(bold('CR'))

    fmt = (
        "{client}'s case #{index} at {system} ({tags}) opened {opened} ({opened_ago}),"
        " updated {updated} ({updated_ago})"
    ) + ("  @{id}" if bot.config.ratbot.apiurl else "")

    event.say(fmt.format(
        client=rescue.client_names, index=rescue.boardindex, tags=", ".join(tags),
        opened=format_timestamp(rescue.createdAt) if rescue.createdAt else '<unknown>',
        updated=format_timestamp(rescue.lastModified) if rescue.lastModified else '<unknown>',
        opened_ago=friendly_timedelta(rescue.createdAt) if rescue.createdAt else '???',
        updated_ago=friendly_timedelta(rescue.lastModified) if rescue.lastModified else '???',
        id=rescue.id or 'pending',
        system=rescue.system or 'an unknown system'
    ))

    # FIXME: Rats/temprats/etc isn't really handled yet.
    if rescue.rats:
        event.qsay("Assigned rats: " + ", ".join(rescue.rats))
    if rescue.unidentifiedRats:
        event.qsay("Assigned unidentifiedRats: " + ", ".join(rescue.unidentifiedRats))
    for ix, quote in enumerate(rescue.quotes):
        event.qsay('[{ix}]{quote}'.format(ix=ix, quote=quote))


@command('clear', aliases=['close'])
@bind('<rescue:rescue:exists>', "Marks a case as closed.")
def cmd_clear(event, rescue):
    """
    Mark a case as closed.
    Required parameters: client name or case number.
    """
    rescue.open = False
    rescue.active = False
    # FIXME: Should have better messaging
    event.qsay("Case {rescue.client_name} is cleared".format(rescue=rescue))
    rescue.board.remove(rescue)
    # FIXME
    # save_case_later(
    #     bot, rescue,
    #     "API is still not done with clearing case {!r}; continuing in background.".format(trigger.group(3))
    # )


@command('list')
@bind('[*options=inactive/names/ids]', "Lists cases, with possible OPTIONS")
@bind('[<?shortoptions:str?-OPTIONS>]', "Lists cases, with possible OPTIONS")
@doc(
    "Lists cases that the bot is currently aware of.  By default, lists only active cases, but you can change this by"
    " specifying OPTIONS."
    "\nNew-style options: Specify 'inactive' to include inactive cases in the list, 'names' to include CMDR names in "
    " the listing, or 'ids' to include APIs in the list.  You may specify multiple options."
    "\nOld-style options: -i is equivalent to inactive, -n is equivalent to names, -@ is equivalent to ids.  These can"
    " be combined (e.g. -in@)"
)
def cmd_list(event, shortoptions=None, options=None):
    if shortoptions:
        if shortoptions[0] != '-':
            raise UsageError("Unknown list option '{}'".format(shortoptions))
        show_ids = '@' in shortoptions and bot.config.ratbot.apiurl is not None
        show_inactive = 'i' in shortoptions
        show_names = 'n' in shortoptions
    else:
        options = set(option.lower() for option in options) if options else set()
        show_ids = 'ids' in options
        show_inactive = 'inactive' in options
        show_names = 'names' in options
    attr = 'client_names' if show_names else 'client_name'

    board = event.bot.data['ratboard']['board']

    def _keyfn(rescue):
        return not rescue.codeRed, rescue.boardindex

    with board:
        actives = list(filter(lambda x: x.active, board.rescues))
        actives.sort(key=_keyfn)
        inactives = list(filter(lambda x: not x.active, board.rescues))
        inactives.sort(key=_keyfn)

    def format_rescue(rescue):
        cr = "(CR)" if rescue.codeRed else ''
        id = ""
        if show_ids:
            id = "@" + (rescue.id if rescue.id is not None else "none")
        return "[{boardindex}{id}]{client}{cr}".format(
            boardindex=rescue.boardindex,
            id=id,
            client=getattr(rescue, attr),
            cr=cr
        )

    output = []
    for name, cases, expand in (('active', actives, True), ('inactive', inactives, show_inactive)):
        if not cases:
            output.append("No {name} cases".format(name=name))
            continue
        num = len(cases)
        s = 's' if num != 1 else ''
        t = "{num} {name} case{s}".format(num=num, name=name, s=s)
        if expand:
            t += ": " + ", ".join(format_rescue(rescue) for rescue in cases)
        output.append(t)
    event.qsay("; ".join(output))


@command('grab')
@bind('<client:str?client nickname>')
def cmd_grab(event, client):
    """
    Grab the last line the client said and add it to the case.
    required parameters: client name.
    """
    result = event.bot.data['ratboard']['history'].get(client.lower())

    if not result:
        # If this were to happen, somebody is trying to break the system.
        # After all, why make a case with no information?
        return event.reply(client + ' has not spoken recently.')
    client, line = result

    result = append_quotes(bot, client, line, create=True)
    if not result:
        return event.reply("Case was not found and could not be created.")

    event.qsay(
        "{rescue.client_name}'s case {verb} with: \"{line}\"  ({tags})"
        .format(
            rescue=result.rescue, verb='opened' if result.created else 'updated', tags=", ".join(result.tags()),
            line=result.added_lines[0]
        )
    )
    # FIXME
    # save_case_later(
    #     event.bot, result.rescue,
    #     "API is still not done with grab for {rescue.client_name}; continuing in background.".format(rescue=result.rescue)
    # )


@command('inject', aliases=['open'])
@bind('<result:fullrescue:create?client> <line:line?message>', "Opens a new text for CLIENT with the opening MESSAGE.")
def cmd_inject(event, result, line):
    result = append_quotes(bot, result, line, create=True)

    event.qsay(
        "{rescue.client_name}'s case {verb} with: \"{line}\"  ({tags})"
        .format(
            rescue=result.rescue, verb='opened' if result.created else 'updated', tags=", ".join(result.tags()),
            line=result.added_lines[0]
        )
    )
    # FIXME
    # save_case_later(
    #     event.bot, result.rescue,
    #     "API is still not done with inject for {rescue.client_name}; continuing in background.".format(rescue=result.rescue)
    # )


@command('sub')
@bind('<rescue:rescue:exists> <lineno:int:0?line number>', "Remove a quote from a case.")
@bind('<rescue:rescue:exists> <lineno:int:0?line number> <line:line?replacement text>', "Changes a quote in a case.")
@doc("Removes or replaces a quote in a case.  The first quote in a case is quote 0.")
def cmd_sub(event, rescue, lineno, line=None):
    if lineno >= len(rescue.quotes):
        event.qreply('Case only has {} line(s)'.format(len(rescue.quotes)))
        return
    if not line:
        if len(rescue.quotes) == 1:
            event.qreply("Can't remove the last line of a case.")
            return
        rescue.quotes.pop(lineno)
        event.say("Deleted line {}".format(lineno))
    else:
        rescue.quotes[lineno] = line
        event.qsay("Updated line {}".format(lineno))
    # FIXME
    # save_case_later(bot, rescue)


def cmd_toggle_flag(event, rescue, attr, text=None, truestate=None, falsestate=None):
    """
    Toggles a rescue flag.

    If `text` is not `None`, responds with ``text.format(rescue=rescue, state=state)`` where ``*state*`` corresponds to
    `truestate` or `falsestate` based on the new attr value.

    Otherwise, responds with ``truestate.format(rescue=rescue)`` or ``falsestate.format(rescue=rescue)``

    :param event: IRC event
    :param rescue: Rescue
    :param attr: Attribute to be toggled.
    :param text: Text to be formatted.
    :param truestate: Text to be included in text's {state} if text is not None, or the entire message displayed if
        text is None.  Shown if the new attr value is True.
    :param falsestate: Text to be included in text's {state} if text is not None, or the entire message displayed if
        text is None.  Shown if the new attr value is False.
    :return:
    """
    result = not getattr(rescue, attr)
    setattr(rescue, attr, result)

    state = truestate if result else falsestate

    if text:
        return event.qsay(text.format(rescue=rescue, state=state))
    else:
        return event.qsay(state.format(rescue=rescue))
from_chain(
    functools.partial(
        cmd_toggle_flag,
        text="{rescue.client_name}'s case is now {state}", truestate="active", falsestate="inactive"
    ),
    command('active', aliases=['activate', 'inactive', 'deactivate']),
    bind("<rescue:rescue:exists>", "Toggles a rescue between active and inactive status.")
)
from_chain(
    functools.partial(
        cmd_toggle_flag,
        text="{rescue.client_name}'s case is now {state}", truestate="epic", falsestate="not as epic"
    ),
    command('epic', aliases=['unepic']),
    bind("<rescue:rescue:exists>", "Toggles a rescue between active and inactive status.")
)


def cmd_change_rats(event, rescue, rats, remove=False, fr=False):
    """
    Changes assigned rats on a case.

    :param event: IRC Event
    :param rescue: Rescue
    :param rats: Rats to add/remove
    :param remove: True if the rats should be removed rather than added.
    :param fr: True to send a FR message to client (only if not Quiet and in a channel.
    :return:
    """
    joined_rats = ", ".join(rats)
    if remove:
        rescue.rats -= set(rats)
        event.qsay("Removed from {rescue.client_name}'s case: {rats}".format(joined_rats))
    else:
        rescue.rats |= set(rats)
        if fr and not event.quiet and event.channel:
            event.reply(
                "Please add the following rat(s) to your friends list: {rats}"
                .format(rats=joined_rats), reply_to=rescue.client_name
            )
            fact = find_fact(event.bot, 'xfr' if rescue.platform == 'xb' else 'pcfr')
            if fact:
                event.reply(fact.message, reply_to=rescue.client_name)
        else:
            event.qsay("Assigned {rats} to {rescue.client_name}".format(rats=joined_rats, rescue=rescue))
    # FIXME
    # save_case_later(event.bot, rescue)
from_chain(
    functools.partial(cmd_change_rats, remove=False, fr=False),
    command('assign', aliases=['add', 'go']),
    bind('<rescue:rescue:exists> <+rats>', "Assign rats to the selected case.")
)
from_chain(
    functools.partial(cmd_change_rats, remove=False, fr=True),
    command('assignfr', aliases=['frassign', 'fradd', 'addfr', 'gopher']),
    bind('<rescue:rescue:exists> <+rats>', "Assign rats to the selected case and instruct client to FR them.")
)
from_chain(
    functools.partial(cmd_change_rats, remove=True),
    command('unassign', aliases=['deassign', 'rm', 'remove', 'standdown']),
    bind('<rescue:rescue:exists> <+rats>', "Remove rats from the selected case.")
)


@command('codered', aliases=['casered', 'cr'])
@bind('<rescue:rescue:exists>', "Toggles a case's Code Red status.  (Setting CR may trigger some people's highlights!)")
def cmd_codered(event, rescue):
    """
    Toggles the code red status of a case.
    A code red is when the client is so low on fuel that their life support
    system has failed, indicated by the infamous blue timer on their HUD.
    """
    rescue.codeRed = not rescue.codeRed
    if rescue.codeRed:
        event.qsay('CODE RED! {rescue.client_name} is on emergency oxygen.'.format(rescue=rescue), transform=False)
        if rescue.rats and not event.quiet:
            event.say(", ".join(rescue.rats) + ": This is your case!")
    else:
        event.qsay('{rescue.client_name}\'s case is no longer CR.'.format(rescue=rescue))
    # FIXME
    # save_case_later(bot, rescue)


def cmd_platform(event, rescue, platform):
    """
    Sets a case platform to PC or xbox.
    """
    rescue.platform = platform
    event.qsay(
        "{rescue.client_name}'s platform set to {platform}".format(rescue=rescue, platform=rescue.platform.upper())
    )
    # FIXME
    # save_case_later(
    #     event.bot, rescue,
    #     (
    #         "API is still not done updating platform for {rescue.client_name}; continuing in background."
    #         .format(rescue=rescue)
    #     )
    # )
from_chain(
    functools.partial(cmd_platform, platform='pc'),
    command(name='pc'),
    bind('<rescue:rescue:exists>', "Sets a case's platform to XBox")
)
from_chain(
    functools.partial(cmd_platform, platform='xb'),
    command(name='xbox', aliases=[Pattern('xb(?:ox)?(?:-?(?:1|one))?')]),
    bind('<rescue:rescue:exists>', "Sets a case's platform to XBox"),
    doc("(Note that various alternate forms are accepted; e.g. xb1, xbox-1, xbone...)")
)


@command('system', aliases=['sys', 'loc', 'location'])
@bind('<rescue:rescue:exists> <system:line>', "Sets a rescue's location.")
@with_session
def cmd_system(event, rescue, system, db=None):
    # Try to find the system in EDSM.
    fmt = "Location of {rescue.client_name} set to {rescue.system}"
    result = db.query(ratlib.db.Starsystem).filter(ratlib.db.Starsystem.name_lower == system.lower()).first()
    if result:
        system = result.name
    else:
        fmt += "  (not in EDSM)"
    rescue.system = system
    event.qsay(fmt.format(rescue=rescue))
    # FIXME
    # save_case_later(
    #     event.bot, rescue,
    #     (
    #         "API is still not done updating system for {rescue.client_name}; continuing in background."
    #         .format(rescue=rescue)
    #     )
    # )


@command('commander', aliases=['cmdr'])
@bind('<rescue:rescue:exists> <commander:line>', "Sets the client's in-game (CMDR) name.")
def cmd_commander(event, rescue, commander):
    """
    Sets a client's in-game commander name.
    required parameters: Client name or case number, commander name
    """
    with rescue.change():
        rescue.client['CMDRname'] = commander

    event.qsay("Client for case {rescue.boardindex} is now CMDR {commander}".format(rescue=rescue, commander=commander))
    # FIXME
    # save_case_later(
    #     event.bot, rescue,
    #     (
    #         "API is still not done updating system for {rescue.client_name}; continuing in background."
    #         .format(rescue=rescue)
    #     )
    # )
