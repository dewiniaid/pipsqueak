"""
board.py - Fuel Rats Cases module.

Copyright 2015, Dimitri "Tyrope" Molenaars <tyrope@tyrope.nl>
Licensed under the Eiffel Forum License 2.

This module is built on top of the Sopel system.
http://sopel.chat/
"""
import abc
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
import json
import pprint
from sqlalchemy import orm
import aiohttp
from aiohttp.websocket import CLOSE_UNSUPPORTED_DATA, CLOSE_OK, CLOSE_POLICY_VIOLATION

import ircbot
import ircbot.commands
from ircbot.commands.exc import *

from ratbot import *
from ratbot.facts import find_fact
from ratlib.db import with_session, models
from ratlib import friendly_timedelta, format_timestamp
from ratlib.autocorrect import correct
from ratlib.starsystem import scan_for_systems
from ratlib.api.props import *
from . import auth

import asyncio
import collections

bold = lambda x: x  # PLACEHOLDER  FIXME

logger = logging.getLogger(__name__)

command = functools.partial(command, category='RESCUES')

Platform = collections.namedtuple('Platform', ['name', 'regex', 'pattern', 'detect'])
PLATFORMS = {}
for platform, name, regex in (
    ('pc', 'PC', 'pc'),
    ('xb', 'XBox', 'xb(?:ox)?(?:-?(?:1|one))?')
):
    PLATFORMS[platform] = Platform(name, regex, re.compile(regex, flags=re.IGNORECASE), re.compile(
        """
        (?:[^\w-]|\A)  # Beginning of line, or non-hyphen word boundary
        {}             # ... followed by case's detection pattern (inserted by format)
        (?:[^\w-]|\Z)  # End of line, or non-hyphen word boundary
        """.format(regex), flags=re.IGNORECASE | re.VERBOSE

    ))
del platform, name, regex


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
        self.open = 'closed_only' not in options
        self.closed = not self.open or 'closed' in options
        self.create = 'create' in options
        self.boardattrs = []
        if self.closed:
            self.boardattrs.append('board_closed')
        if self.open:
            self.boardattrs.append('board')

        if not self.open and self.create:
            raise ParseError("Cannot have a rescue parameter that creates only closed cases.")

    def parse(self, event, value):
        for attr in self.boardattrs:
            result = event.bot.data['ratboard'][attr].find(value, create=self.create)
            if result.rescue is not None:
                break
        if result.rescue is None:
            if self.create:
                raise UsageError("Case {} was not found and could not be created.".format(value), final=True)
            else:
                raise UsageError("Case {} was not found.".format(value), final=True)
        return result if self.fullresult else result.rescue


# noinspection PyAttributeOutsideInit
class RatboardConfig(ircbot.ConfigSection):
    def read(self, section):
        self.signal = re.compile(section.get('signal', 'ratsignal'), re.IGNORECASE)
        self.case_pool_size = section.getint('case_pool_size', 10)
        self.case_history = section.getint('case_history', 10)
        self.client_history = section.getint('client_history', 5000)
        self.apidebug = section.getboolean('apidebug', True)

bot.config.section('ratboard', RatboardConfig)


@prepare
def setup(bot):
    bot.data['ratboard'] = {
        'history': collections.OrderedDict(),
        'board': RescueBoard(maxpool=bot.config.ratboard.case_pool_size),
        'board_closed': ClosedRescueBoard(),
        'ws': None
    }

    if bot.config.ratbot.wsurl:
        ws = bot.data['ratboard']['ws'] = WSAPI(bot.config.ratbot.wsurl, debug=bot.config.ratboard.apidebug)
        asyncio.get_event_loop().call_soon(ws.start)
    asyncio.get_event_loop().run_until_complete(refresh_cases(bot))


class WSAPI:
    """Websocket API layer"""
    reference_attr = 'return'  #: Attribute of metadata that will contain our request ID.
    _default = object()

    def __init__(self, url, *, loop=None, default_timeout=30.0, debug=False):
        """
        :param url: URL we connect to.
        :param loop: AIO event loop.  `None` means it will be autodetected.  Used as the default loop for .start()
        :param default_timeout: Default timeout for requests expecting a response.  `None` disables.
        """
        self.url = url  #: URL for this websocket
        self.connection = None  #: Pointer to our current connection.  None if not connected.
        self.reconnect = True  #: If True, automatically reconnect upon disconnecting.
        self.task = None  #: Handle to the task created by :meth:`start`
        self.logger = logger.getChild('WSAPI')  #: Logger.
        self.loop = loop  #: Event loop
        self.debug = debug

        self.default_timeout = default_timeout  #: How long we wait for a response to any one given request (by default)

        self.connected_future = asyncio.Future()  #: Resolved when we connect and finish initialization tasks.

        #: Stores result futures.  Each item is a tuple of 1 or 2 elements: the future and its asyncio.timeout() future
        self.result_futures = {}
        self._counter = itertools.count(1)  # Result ID counter

    def result_id(self):
        """
        Returns the next valid result_id.  This gets its own method primarily for the benefit of potential subclasses.
        """
        return next(self._counter)

    def start(self, loop=None):
        self.loop = loop = loop or self.loop or asyncio.get_event_loop()
        if self.task:
            raise ValueError("WSAPI task already started.")
        task = loop.create_task(self.dispatcher())

    async def _terminate(self, reason, code=CLOSE_OK):
        """
        Terminate a connection due to error.

        :param reason: Reason for termination
        :param code: Websocket close code.
        """
        self.connected_future = asyncio.Future()
        self.logger.error("Terminating connection: {}".format(reason))
        result = await self.connection.close(code)
        self.connection = None
        return result

    def log_request(self, obj, prefix='', **kwargs):
        """
        Pretty formats obj, for debugging.

        :param obj: Object to pretty-print
        :param prefix: Prefix that will be added to each line.
        :param kwargs: Arguments to pass to pprint.pformat()
        :return: Formatted str
        """
        if not self.debug:
            return
        kwargs.setdefault('indent', 1)
        kwargs.setdefault('compact', False)
        result = pprint.pformat(obj, **kwargs)
        for line in result.split("\n"):
            self.logger.debug(prefix + line)

    async def dispatcher(self):
        """Handles dispatching requests."""
        self.logger.info("Connecting...")
        session = aiohttp.ClientSession()
        first_run = True

        while self.reconnect or first_run:
            first_run = False
            try:
                async with session.ws_connect(self.url) as self.connection:
                    self.logger.info("Connected.")
                    self.connected_future.set_result(True)
                    async for msg in self.connection:
                        # Validate that the message is something we can handle.
                        if msg.tp != aiohttp.MsgType.text:
                            self.logger.debug(
                                "Received message type {} ({})".format(msg.tp.value, msg.tp.name)
                            )
                            if msg.tp == aiohttp.MsgType.binary:
                                await self._terminate("received unexpected binary data.", CLOSE_UNSUPPORTED_DATA)
                                break
                            continue
                        # Construct and validate JSON
                        try:
                            data = json.loads(msg.data)
                        except ValueError as ex:
                            await self._terminate(
                                "received bad JSON message: {}".format(str(ex)), CLOSE_POLICY_VIOLATION
                            )
                            break
                        if not isinstance(data, dict):
                            await self._terminate("JSON message was not a dict.", CLOSE_POLICY_VIOLATION)
                            break
                        if 'error' in data:
                            self.logger.error("API Error: " + repr(data['error']))
                        if 'meta' not in data:
                            await self._terminate("Message from API lacked metadata.", CLOSE_POLICY_VIOLATION)
                            break
                        result_id = data['meta'].get(self.reference_attr)
                        # Dispatch result.
                        if result_id is None:
                            self.logger.debug("[*] Received Message.")
                            tag = '*'
                        else:
                            tag = result_id
                            self.logger.debug("[{}] Received Response.".format(tag))
                        self.log_request(data, prefix="[{}] << ".format(tag))
                        if result_id is None:
                            continue
                        try:
                            result = self.result_futures.get(result_id)
                        except TypeError as ex:  # Unhashable type, probably.
                            self.logger.warn("Received a result with unhashable result_id: '{}'".format(result_id))
                        if result is None:
                            self.logger.warn("Received a result_id that we weren't expecting: '{}'".format(result_id))
                            continue
                        result.set_result(data)
            except Exception:
                self.logger.exception("Error in WS event loop, terminating connection.")
                raise
            finally:
                try:
                    if self.connection and not self.connection.closed:
                        self.connection.close()
                finally:
                    self.connection = None
                    if self.connected_future.done():
                        self.connected_future = asyncio.Future()

    async def send(self, data):
        """
        Sends data to the API.  Does not notify upon response.

        :param data: Message to send.  Must be a dict.
        """
        await self.connected_future
        self.log_request(data, prefix="[*] >> ")
        self.connection.send_str(json.dumps(data))

    def _remove_result(self, result_id, *unused):
        del self.result_futures[result_id]

    async def request(self, data, timeout=_default):
        """
        Sends data to the API.  Returns a future that resolves when the timeout expires or the API responds.

        :param data: Message to send.  Must be a dict.
        :param timeout: Timeout.  `None` disables.  Uses :attr:`default_timeout` by default.
        """
        if timeout is self._default:
            timeout = self.default_timeout
        if 'meta' not in data:
            data['meta'] = {}
        # Assign a result ID
        if self.reference_attr in data['meta']:
            result_id = data['meta'][self.reference_attr]
        else:
            result_id = data['meta'][self.reference_attr] = self.result_id()
        assert result_id not in self.result_futures

        if timeout:
            handler = aiohttp.Timeout(timeout)  # asyncio.timeout is actually missing -- python bug until 3.5.2
        else:
            handler = contextlib.ExitStack()

        try:
            with handler:
                await self.connected_future
                self.logger.debug("[{}] Submitted Request.".format(result_id))
                self.result_futures[result_id] = future = asyncio.Future()
                self.log_request(data, prefix="[{}] >> ".format(result_id))
                self.connection.send_str(json.dumps(data))
                return await future
        except asyncio.TimeoutError:
            self.logger.debug("[{}] Request Timed Out ({} seconds.)".format(result_id, timeout))

        finally:
            del self.result_futures[result_id]


FindRescueResult = collections.namedtuple('FindRescueResult', ['rescue', 'created'])


class RescueBoardBase(metaclass=abc.ABCMeta):
    """
    Manages all attached cases, including API calls.
    """
    INDEX_TYPES = {
        'boardindex': operator.attrgetter('boardindex'),
        'id': operator.attrgetter('id'),
        'clientnick': lambda x: None if not x.client or not x.client.get('nickname') else x.client['nickname'].lower(),
        'clientcmdr': lambda x: None if not x.client or not x.client.get('CMDRname') else x.client['CMDRname'].lower(),
    }

    def __init__(self):
        self.indexes = {k: {} for k in self.INDEX_TYPES.keys()}

    def add(self, rescue):
        """
        Adds the selected case to our indexes.
        """
        assert rescue.board is None, "Rescue is already assigned."
        assert rescue.boardindex is None, "Rescue already has a boardindex."
        # Assign an boardindex
        rescue.board = self
        rescue.boardindex = self.alloc_index()

        # Add to indexes
        for index, fn in self.INDEX_TYPES.items():
            # FIXME: This will fail horribly if the function raises
            key = fn(rescue)
            if key is None:
                continue
            if key in self.indexes[index]:
                logger.warn("Key {key!r} is already in index {index!r}".format(key=key, index=index))
                continue
            self.indexes[index][key] = rescue

    def remove(self, rescue):
        """
        Removes the selected case from our indexes.
        """
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
        self.free_index(rescue.boardindex)
        rescue.boardindex = None
        rescue.board = None

    @contextlib.contextmanager
    def change(self, rescue):
        """
        Returns a context manager that snapshots case attributes and updates the indexes with any relevant changes.

        Usage Example:
        ```
        with board.change(rescue):
            rescue.client['CMDRname'] = cmdrname
        """
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
        if 'boardindex' in self.indexes:
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

        if not search or not isinstance(search, str):
            return FindRescueResult(None, None)

        if 'id' in self.indexes and search[0] == '@':
            rescue = self.indexes['id'].get(search[1:], None),
            return FindRescueResult(rescue, False if rescue else None)

        clientname = search.lower()
        rescue = None
        if rescue is None and 'clientnick' in self.indexes:
            rescue = self.indexes['clientnick'].get(clientname)
        if rescue is None and 'clientcmdr' in self.indexes:
            rescue = self.indexes['clientcmdr'].get(clientname)
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

    @abc.abstractmethod
    def alloc_index(self):
        pass

    @abc.abstractmethod
    def free_index(self, index):
        pass


class RescueBoard(RescueBoardBase):
    MAX_POOLED_CASES = 10

    def __init__(self, maxpool=None):
        super().__init__()
        if maxpool is None:
            maxpool = self.MAX_POOLED_CASES

        # Boardindex pool
        self.maxpool = maxpool
        self.counter = itertools.count(start=self.maxpool)
        self.pool = collections.deque(range(0, self.maxpool))

    def alloc_index(self):
        """Retrieves a boardindex from the pool."""
        try:
            return self.pool.popleft()
        except IndexError:
            return next(self.counter)

    def free_index(self, index):
        """Returns a boardindex back to the pool."""
        if index < self.maxpool:
            self.pool.append(index)
        if not self.indexes['boardindex']:  # Board is clear.
            self.counter = itertools.count(start=self.maxpool)


class ClosedRescueBoard(RescueBoardBase):
    """Represents a history of recently closed rescues."""
    INDEX_TYPES = {
        'boardindex': operator.attrgetter('boardindex'),
        'id': operator.attrgetter('id'),
    }

    def __init__(self, maxval=-10, maxq=1000):
        """
        Creates a new :class:`ClosedRescueBoard`.

        :param maxval: Our target number of rescues to keep.  If negative, we start at -1 and count down for boardindex.
        :param maxq: If we're set as offline, the maximum number of rescues we'll keep.

        When rescues are added to added to a ClosedRescueBoard, they get assigned a slot between 1 and 'maxval'.

        If we're in offline mode, any rescue already in that slot is pushed into the offline queue.  If we're in
        online mode, the rescue is removed entirely instead.

        If running API-less, we should be "online"
        """
        super().__init__()
        assert maxval
        self.maxval = maxval
        self.maxsize = maxq
        self.lastval = 0
        self.step = 1 if maxval > 0 else -1
        self.offline = False
        self.offline_queue = collections.deque(maxlen=maxq)

    def alloc_index(self):
        self.lastval += self.step
        if (self.lastval*self.step) > (self.maxval*self.step):
            self.lastval = self.step
        return self.lastval

        oldrescue = self.indexes['boardindex'].get(index)
        if oldrescue:
            self.remove(oldrescue)
            if self.offline:
                self.offline_queue.append(oldrescue)

        return index

    def free_index(self, index):
        pass  # noop

    def find(self, search, create=False):
        return super().find(search, create=False)  # Never can create a closed case via a search.

    def rescue_sort_key(self, rescue):
        """Works as a key function for sorting rescues in order of most recently added to least."""
        offset = self.maxval - self.lastval
        return (rescue.boardindex + offset) % self.maxval


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
        self._lock = asyncio.locks.Lock()

    def __aenter__(self):
        return self._lock.__aenter__()

    def __aexit__(self, exc_type, exc_val, exc_tb):
        return self._lock.__aexit__(exc_type, exc_val, exc_tb)

    def _check_if_locked(self):
        """Squawks loudly if called and we're not currently locked."""
        if not self._lock.locked():
            logger.warning("Performing an operation on a rescue that expected us to be locked, but we're not.")

    def change(self):
        """
        Convenience shortcut for performing safe attribute changes (that also update indexes).  Same as

        ```
        with rescue.change():
            rescue.client['CMDRname'] = 'Foo'
        ```

        If the rescue is not attached to a board, this returns a dummy context manager that does nothing.
        """
        if self.board:
            return self.board.change(self)

        @contextlib.contextmanager
        def _dummy():
            yield self
        return _dummy()

    def refresh(self, data, merge=True, delta=True, _uselock=True):
        """
        Refreshes this `Rescue` from attributes in the JSON `data`

        :param data: JSON data
        :param merge: Whether to discard changes that conflict with our own pending changes.
        :param delta: Whether or not to return a delta summarizing all changes.
        :return:
        """
        if not _uselock:
            self._check_if_locked()
        if delta:
            before = self.dump(full=True)

        for prop in self._props:
            if isinstance(prop, InstrumentedProperty):
                prop.read(self, data, merge=merge)
                continue
            if merge and prop in self._changed:
                continue  # Ignore incoming data that conflicts with our pending changes.
            prop.read(self, data)

        if delta:
            return self.generate_changeset(before, full=True)

    def generate_changeset(self, before, after=None, full=True, props=None):
        """
        Given `before` and `after`, which are both dictionaries produced by :meth:`dump`, generate a summary of
        noteworthy changes.

        :param before: Snapshot of state before change.
        :param after: Snapshot of state after change.  If `None`, will be produced based on current state.
        :param full: Passed to :meth:`dump` if creating a snapshot.
        :param props: Passed to :meth:`dump` if creating a snapshot.
        """
        if after is None:
            after = self.dump(full, props)
        return self._generate_changeset(before, after)

    @classmethod
    def _generate_changeset(cls, before, after):
        """Implements generate_changeset"""
        def _diff(_before, _after):
            _result = dict((k, v) for k, v in _after.items() if _before.get(k) != v)
            _result.update(zip((k for k in _before if k not in _after), itertools.repeat(None)))
            return _result

        result = _diff(before, after)

        # Handle the set-like attributes that... aren't
        # These might not be equal due to ordering differences, but they're actually sets so order is
        # irrelevant
        for attr in ('unidentifiedRats', 'rats'):
            if attr not in result:
                continue
            b = set(before.get(attr) or [])
            a = set(after.get(attr) or [])
            if a == b:
                del result[attr]
            else:
                result[attr] = a

        # Handle CMDRname
        if 'client' in result:
            result['client'] = _diff(before.get('client', {}), after.get('client', {}))

        # Handle quotes if it's a simple append.
        # We can tell this by: after quotes being longer then before quotes, and the first len(a) elements matching.
        if 'quotes' in result:
            result['quotes_appended'] = None
            a = after.get('quotes') or []
            if a:
                b = before.get('quotes') or []
                if len(b) < len(a):
                    if all(a[ix] == b[ix] for ix in range(len(b))):  # Safe for zero-length.
                        result['quotes_appended'] = b[len(a):]
        return result

    @classmethod
    def load(cls, json, inst=None):
        """
        Creates a case from a JSON dict.
        """
        if inst is None:
            inst = cls()
        inst.refresh(json)
        return inst

    def _normalize_props(self, full, props=None):
        """
        Compute a list of changed properties for various methods.

        :param full: If True, returns all properties.
        :param props: If `full` is True, this is ignored.  Otherwise, this overrides the value of `self._changed`
        :return: A set of properties

        `props` can contain properties by name or the actual property objects.
        """
        if full:
            return self._props
        if props is None:
            return self._changed
        return set(prop if prop in self._props else self._propnames[prop] for prop in props)

    def _dump(self, props):
        """Internal implementation of dump.  Does not normalize props."""
        rv = {}
        for prop in props:
            prop.write(self, rv)
        return rv

    def dump(self, full=False, props=None):
        """
        Dumps this rescue to a dictionary.

        :param full: If True, forces a full save regardless of changed properties.
        :param props: Overrides changed properties if set
        :return: Populated dictionary suitable for JSON serialization.
        """
        return self._dump(self._normalize_props(full, props))

    async def save(self, ws, full=False, props=None):
        """
        Saves this rescue

        :param ws: WSAPI instance to submit the request to.
        :param full: If True, forces a full save regardless of changed properties.
        :param props: Overrides changed properties if set
        :return:
        """
        # Fast touch & exit if ws is None
        if not ws:
            self.touch()
            self.commit()
            return None

        props = self._normalize_props(full, props)
        if not props:
            return  # Nothing to do

        request = {'data': self._dump(props)}
        if self.id:
            request['id'] = self.id
            request['data']['id'] = self.id
            request['action'] = 'rescues:update'
        else:
            request['action'] = 'rescues:create'

        request['data'] = self._dump(props)
        result = await ws.request(request)
        # TODO: Write code that does things if our request does bad things.  Oh, and validate.
        for prop in props:
            prop.commit(self)
        self._changed -= props
        self.refresh(result['data'])
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


async def refresh_cases(bot, rescue=None):
    """
    Grab all open cases from the API so we can work with them.

    :param bot: IRC bot
    :param rescue: Individual rescue to refresh.
    """
    ws = bot.data['ratboard']['ws']
    board = bot.data['ratboard']['board']

    if not ws:
        logging.warn("No websocket configured.  Operating in offline mode.")
        return
    future = ws.request({'action': 'rescues:read', 'data': {'open': True, 'id': '56d4a4852410b38e0490e321'}})
    result = await future   # FIXME: Need individual rescue refresh as well.
    print(repr(result))

    # if rescue:
    #     if not result['data']:
    #         board.remove(rescue)
    #     else:
    #         with rescue.change():
    #             rescue.refresh(result['data'])
    #     return
    if rescue:
        raise NotImplementedError("Support for refreshing individual rescues is currently disabled.")

    # Cases we have but the refresh doesn't.  We'll assume these are closed after winnowing down the list.
    missing = set(board.indexes['id'])

    for case in result['data']:
        id = case['_id']
        missing.discard(id)  # Case still exists.
        # Find our counterpart, which we may or may not have.
        existing = board.indexes['id'].get(id)

        if existing:
            async with existing:
                with existing.change():
                    existing.refresh(case)
                continue
        else:
            board.add(Rescue.load(case))

    for id in missing:
        case = board.indexes['id'].get(id)
        if case:
            board.remove(case)
            bot.data['ratboard']['board_closed'].add(rescue)


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
            # Attempt to detect a platform
            for platform_id, platform in PLATFORMS.items():
                if platform.detect.search(line):
                    platforms.add(platform_id)
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
async def rule_ratsignal(event):
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
    await save_or_complain(event, result.rescue)


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
async def cmd_clear(event, rescue):
    """
    Mark a case as closed.
    Required parameters: client name or case number.
    """
    rescue.open = False
    rescue.active = False
    # FIXME: Should have better messaging
    event.qsay("Case {rescue.client_name} is cleared".format(rescue=rescue))
    rescue.board.remove(rescue)
    event.bot.data['ratboard']['board_closed'].add(rescue)
    await save_or_complain(event, rescue)


@command('list')
@bind('[*options=inactive/names/ids/closed]', "Lists cases, with possible OPTIONS")
@bind('[<?shortoptions:str?-OPTIONS>]', "Lists cases, with possible OPTIONS")
@doc(
    "Lists cases that the bot is currently aware of.  By default, lists only active cases, but you can change this by"
    " specifying OPTIONS."
    "\nNew-style options: Specify 'inactive' to include inactive cases in the list, 'names' to include CMDR names in "
    " the listing, or 'ids' to include APIs in the list.  Specify 'closed' to show recently closed cases instead of"
    " open cases (implies 'inactive').  You may specify multiple options."
    "\nOld-style options: -i is equivalent to inactive, -n is equivalent to names, -@ is equivalent to ids, -c is"
    " equivalent to closed.  These can be combined (e.g. -inc@)"
)
def cmd_list(event, shortoptions=None, options=None):
    if shortoptions:
        if shortoptions[0] != '-':
            raise UsageError("Unknown list option '{}'".format(shortoptions))
        show_ids = '@' in shortoptions
        show_closed = 'c' in shortoptions
        show_inactive = 'i' in shortoptions
        show_names = 'n' in shortoptions
    else:
        options = set(option.lower() for option in options) if options else set()
        show_ids = 'ids' in options
        show_closed = 'closed' in options
        show_inactive = 'inactive' in options
        show_names = 'names' in options
    show_ids = show_ids and (bot.config.ratbot.wsurl is not None)
    show_inactive = show_inactive or show_closed

    attr = 'client_names' if show_names else 'client_name'

    board = event.bot.data['ratboard']['board_closed' if show_closed else 'board']

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

    lists = []
    if show_closed:
        lists.append(('recently closed', True, sorted(board.rescues, key=board.rescue_sort_key)))
    else:
        def _keyfn(rescue):
            return not rescue.codeRed, rescue.boardindex
        lists.append(('active', True, sorted(filter(lambda x: x.active, board.rescues), key=_keyfn)))
        inactives = list(filter(lambda x: not x.active, board.rescues))
        if show_inactive:
            inactives.sort(key=_keyfn)
        lists.append(('inactive', show_inactive, sorted(filter(lambda x: not x.active, board.rescues), key=_keyfn)))

    output = []
    for name, expand, cases in lists:
        if not cases:
            output.append("No {name} cases".format(name=name))
            continue
        num = len(cases)
        s = 's' if num != 1 else ''
        t = "{num} {name} case{s}".format(num=num, name=name, s=s)
        if expand:
            t += ": " + ", ".join(format_rescue(rescue) for rescue in cases)
        output.append(t)

    if show_closed:
        output.append("Use " + event.prefix + "reopen <index> to reopen a closed case.")
    event.qsay("; ".join(output))


@command('reopen')
@bind('<rescue:rescue:closed_only>', "Reopens a recently closed case.  Must be specified by index number.")
@doc(
    "Reopens a recently closed case by specifying its index number.  There must be no open cases for the client."
    "  Closed cases are identified by negative indexes (e.g. -1).  Closed cases cannot be modified without reopening"
    " them first."
)
async def cmd_reopen(event, rescue):
    board = bot.data['ratboard']['board']
    closed_board = bot.data['ratboard']['board_closed']

    if rescue.board is not closed_board:
        logger.warn(
            "Attempting to reopen a case that is not on the closed case board: <{}> {}"
            .format(event.nick, event.message)
        )
        event.reply("I can't reopen that case, because that's not a case that is closed.")
        return

    INDEX_TYPES = {
        'boardindex': operator.attrgetter('boardindex'),
        'id': operator.attrgetter('id'),
        'clientnick': lambda x: None if not x.client or not x.client.get('nickname') else x.client['nickname'].lower(),
        'clientcmdr': lambda x: None if not x.client or not x.client.get('CMDRname') else x.client['CMDRname'].lower(),
    }

    for index, name in (('clientnick', 'Client nickname'), ('clientcmdr', 'Client CMDR name')):
        v = board.INDEX_TYPES[index](rescue)
        if v is None:
            continue
        if v in board.indexes[index]:
            event.reply("Unable to reopen case: {} is in use by an existing open case.")
            return

    rescue.board.remove(rescue)
    board.add(rescue)
    rescue.open = True
    rescue.active = True
    event.qsay(
        "Case for {rescue.client_name} is reopened and reactivated as case #{rescue.boardindex}."
        .format(rescue=rescue)
    )
    await save_or_complain(event, rescue)


@command('grab')
@bind('<client:str?client nickname>')
async def cmd_grab(event, client):
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
    await save_or_complain(event, result.rescue)


@command('inject', aliases=['open'])
@bind(
    '<result:fullrescue:create?client> <line:line?message>',
    "Opens a new case for CLIENT with the opening MESSAGE, or appends the message to their existing case."
)
async def cmd_inject(event, result, line):
    result = append_quotes(bot, result, line, create=True)

    event.qsay(
        "{rescue.client_name}'s case {verb} with: \"{line}\"  ({tags})"
        .format(
            rescue=result.rescue, verb='opened' if result.created else 'updated', tags=", ".join(result.tags()),
            line=result.added_lines[0]
        )
    )
    await save_or_complain(event, result.rescue)


@command('sub')
@bind('<rescue:rescue:exists> <lineno:int:0?line number>', "Remove a quote from a case.")
@bind('<rescue:rescue:exists> <lineno:int:0?line number> <line:line?replacement text>', "Changes a quote in a case.")
@doc("Removes or replaces a quote in a case.  The first quote in a case is quote 0.")
async def cmd_sub(event, rescue, lineno, line=None):
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
    await save_or_complain(event, rescue)


async def cmd_toggle_flag(event, rescue, attr, text=None, truestate=None, falsestate=None):
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
        event.qsay(text.format(rescue=rescue, state=state))
    else:
        event.qsay(state.format(rescue=rescue))
    await save_or_complain(event, rescue)

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


async def cmd_change_rats(event, rescue, rats, remove=False, fr=False):
    """
    Changes assigned rats on a case.

    :param event: IRC Event
    :param rescue: Rescue
    :param rats: Rats to add/remove
    :param remove: True if the rats should be removed rather than added.
    :param fr: True to send a FR message to client (only if not Quiet and in a channel.
    :return:
    """
    # FIXME: API lookups needed to properly assign rat IDs.
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
async def cmd_codered(event, rescue):
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
    await save_or_complain(event, rescue)


async def cmd_platform(event, rescue, platform):
    """
    Sets a case platform to PC or xbox.
    """
    rescue.platform = platform
    event.qsay(
        "{rescue.client_name}'s platform set to {platform}".format(rescue=rescue, platform=rescue.platform.upper())
    )
    await save_or_complain(event, rescue)
    # FIXME
    # save_case_later(
    #     event.bot, rescue,
    #     (
    #         "API is still not done updating platform for {rescue.client_name}; continuing in background."
    #         .format(rescue=rescue)
    #     )
    # )
for platform_id, platform in PLATFORMS.items():
    from_chain(
        functools.partial(cmd_platform, platform=platform_id),
        command(name=platform.name.lower(), aliases=[Pattern(platform.regex)]),
        bind('<rescue:rescue:exists>', "Sets a case's platform to " + platform.name)
    )
del platform_id, platform

@command('system', aliases=['sys', 'loc', 'location'])
@bind('<rescue:rescue:exists> <system:line>', "Sets a rescue's location.")
@with_session
async def cmd_system(event, rescue, system, db=None):
    # Try to find the system in EDSM.
    fmt = "Location of {rescue.client_name} set to {rescue.system}"
    result = db.query(models.Starsystem).filter(models.Starsystem.name_lower == system.lower()).first()
    if result:
        system = result.name
    else:
        fmt += "  (not in EDSM)"
    rescue.system = system
    event.qsay(fmt.format(rescue=rescue))

    await save_or_complain(event, rescue)
    # FIXME
    # save_case_later(
    #     event.bot, rescue,
    #     (
    #         "API is still not done updating system for {rescue.client_name}; continuing in background."
    #         .format(rescue=rescue)
    #     )
    # )

async def save_or_complain(event, rescue, message="Failed to save rescue {rescue.boardindex}"):
    """
    Saves a rescue, or complains about it

    :param bot: Bot instance
    :param rescue: Rescue to save
    :param message: Message to be included in exceptions.  Exception text will be included as well.
    """
    try:
        try:
            async with rescue:
                await rescue.save(event.bot.data['ratboard']['ws'])
        except asyncio.TimeoutError:
            # TODO: Do something about possibly being disconnected.
            raise
        except aiohttp.ClientDisconnectedError:
            # TODO: Do something about possibly being disconnected.
            raise
    except Exception as ex:
        # Hackish, but works, and the builtin Python logging library does the same thing.
        if '{rescue' in message:
            message = message.format(rescue=rescue)
        event.qsay(message + ": " + str(ex))
        logger.exception(message)


@command('commander', aliases=['cmdr'])
@bind('<rescue:rescue:exists> <commander:line>', "Sets the client's in-game (CMDR) name.")
async def cmd_commander(event, rescue, commander):
    """
    Sets a client's in-game commander name.
    required parameters: Client name or case number, commander name
    """
    with rescue.change():
        rescue.client['CMDRname'] = commander

    event.qsay(
        "Client for case {rescue.boardindex} is now CMDR {commander}"
        .format(rescue=rescue, commander=commander)
    )
    await save_or_complain(event, rescue)


def platform_name(platform_id):
    if not platform_id:
        return 'unknown'
    if platform_id in PLATFORMS:
        return PLATFORMS[platform_id].name
    return platform_id


@command('iam')
@bind('', "Shows the CMDR name(s) the bot thinks you are currently operating as.")
@bind('<platform:str> <cmdr:line>', "Sets your CMDR name on the specified platform.")
@auth.requires_account
@with_session
async def cmd_iam(event, platform=None, cmdr=None, account=None, db=None):
    ws = bot.data['ratboard']['ws']
    if platform is None:
        sync_iam(account, db)
        identities = []
        for platform_id in sorted(account.data['iam']):
            iam = account.data['iam'][platform_id]
            identities.append("CMDR {} (on {})".format(iam.name, platform_name(platform_id)))
        if not identities:
            event.qreply("Your CMDR name is not set on any platforms.")
            return
        event.qreply("You are {temporarily}known as {identities}.  Your default platform is {platform}.".format(
            temporarily='' if account.name else 'temporarily ',
            identities=", ".join(identities),
            platform=account.data['iam_platform'] or 'unknown'
        ))
        return
    platform = platform.lower()
    for platform_id, data in PLATFORMS.items():
        if data.pattern.fullmatch(platform):
            break
    else:
        raise UsageError("Unknown platform '{}'".format(platform))

    if account.name is None:
        event.unotice("You are not currently identified with NickServ.  Changes to preferences will not be saved.")

    api_id = None
    warning = None

    if ws:
        # Try to validate their selection
        request = {
            'action': "rats:read",
            'data': {
                'platform': platform_id,
                'CMDRname': cmdr
            },
            'meta': { 'limit': 5 }
        }
        result = await ws.request(request)
        # Deal with API madness.
        matchlevel = 0  # 4 = exact match, 3 = platform mismatch, 2 = closest same platform, 1 = closest platforms
        best_match = None
        for rat in result['data']:
            if rat['CMDRname'].lower() == cmdr.lower():
                if rat['platform'] == platform_id:
                    api_id = rat['_id']
                    best_match = rat
                    matchlevel = 4
                    break
                best_match = rat
                matchlevel = 3
                continue
            if matchlevel < 2 and rat['platform'] == platform_id:
                best_match = rat
                matchlevel = 2
                continue
            if not matchlevel:
                matchlevel = 1
                best_match = rat
        if matchlevel == 3:
            warning = (
                "A rat with that name was found, but on platform '{}'."
                "  Rescues may not be linked to you.".format(platform_name(best_match['platform']))
            )
        elif matchlevel == 2:
            warning = (
                "No rat with that name was found.  The closest match on that platform is '{}'."
                "  Rescues may not be linked to you.".format(best_match['CMDRname'])
            )
        elif matchlevel == 1:
            warning = (
                "No rat with that name was found.  The closest match on platform '{}' is '{}'."
                "  Rescues may not be linked to you.".format(
                    platform_name(best_match['platform']), best_match['CMDRname']
                )
            )
        elif not matchlevel:
            warning = "No rat with that name was found.  Rescues may not be linked to you."
    if account.name:
        instance = account.ensure_instance(db)
        db.add(instance)
        sync_iam(account, db, platform_id, instance)
        iam = models.AccountIAm(account_id=instance.id, platform=platform_id, api_id=api_id, name=cmdr)
        iam = db.merge(iam)
        db.commit()
        orm.make_transient(iam)
    else:
        sync_iam(account, db, platform_id)
        iam = models.AccountIAm(platform=platform_id, api_id=api_id, name=cmdr)
    orm.make_transient(iam)
    account.data.setdefault('iam_platform', platform_id)
    account.data.setdefault('iam', {})
    account.data['iam'][platform_id] = iam

    message = "You are now CMDR {} on {}.".format(cmdr, platform_name(best_match['platform']))
    if warning:
        message += "  " + warning
    event.qreply(message)

@command('imon', aliases=['iamon'])
@bind('', "Shows the platform the bot thinks you are currently playing on.")
@bind('<platform:str>', "Sets your current platform.")
@auth.requires_account
@with_session
async def cmd_imon(event, platform=None, account=None, db=None):
    sync_iam(account, db)
    if not account.data['iam']:
        event.qreply("You have no identities configured.  Configure one with {}iam first.".format(event.prefix))
        return
    if platform is None:
        event.qreply("Your current plaform is {}.".format(platform_name(account.data['iam_platform'])))
        return
    platform = platform.lower()
    for platform_id, data in PLATFORMS.items():
        if data.pattern.fullmatch(platform):
            break
    else:
        raise UsageError("Unknown platform '{}'".format(platform))
    if platform_id not in account.data['iam']:
        event.qreply(
            "You do not have an identity configured on that platform.  Configure one with {}iam first."
            .format(event.prefix)
        )
        return
    account.data['iam_platform'] = platform
    if account.name is None:
        event.unotice("You are not currently identified with NickServ.  Changes to preferences will not be saved.")
    if account.name:
        instance = account.ensure_instance(db)
        instance.iam_platform = platform
        db.add(instance)
        db.commit()
    event.qreply("Your current platform is now {}.".format(platform_name(platform)))


def sync_iam(account, db, platform_id=None, instance=None):
    """Retrieves IAM data from the database."""
    account.data.setdefault('iam', {})
    if not instance:
        instance = account.get_instance(db)
    if not account.data.get('iam_platform'):
        if instance and instance.iam_platform:
            account.data['iam_platform'] = instance.iam_platform
        else:
            account.data['iam_platform'] = platform_id
            if instance:
                instance.iam_platform = platform_id
    if instance:
        for platform_id, iam in instance.iam.items():
            orm.make_transient(iam)
            account.data['iam'][platform_id] = iam

