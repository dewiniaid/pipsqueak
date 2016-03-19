import datetime
import concurrent.futures
import sys
import logging

import ircbot
from ircbot.commands.commands import Pattern, from_chain, bind, alias, doc
import ircbot.modules.core
from pydle import coroutine
import tornado.platform.asyncio

import ratlib
import ratlib.db
import ratlib.starsystem

logger = logging.getLogger(__name__)


__all__ = [
    'bot', 'command', 'rule', 'RatbotConfig', 'setup', 'start', 'prepare',
    'Pattern', 'from_chain', 'bind', 'alias', 'doc', 'coroutine'
]
bot = None
rule = None
command = None
pending = []


def prepare(fn):
    """Decorates functions to be called when we start."""
    logger.debug("Preparing " + fn.__module__ + "." + fn.__name__)
    pending.append(fn)
    return fn


class RatbotConfig(ircbot.ConfigSection):
    def read(self, section):
        self.apiurl = section.get('apiurl', None)
        self.wsurl = section.get('wsurl', None)
        self.alembic = section.get('alembic', 'alembic.ini')
        self.debug_sql = section.getboolean('debug_sql', False)
        self.quiet_command = section.get('quiet_command', '@')

        self.database = section['database']

        # TODO: Make these their own config
        self.edsm_url = section.get('edsm_url', "http://www.edsm.net/api-v1/systems?coords=1")
        self.edsm_maxage = section.getint('edsm_maxage', 12*60*60)
        self.edsm_autorefresh = section.getint('edsm_autorefresh', 4*60*60)

        self.version_string = section.get('version_string')
        self.version_file = section.get('version_file')
        self.version_cmd = section.get('version_cmd')
        self.version_git = section.get('version_git', 'git')


class Event(ircbot.Event):
    @property
    def quiet(self):
        return self.prefix == self.bot.config.ratbot.quiet_command

    def qsay(self, *args, **kwargs):
        """
        Same as say() if quiet is not set or there's no channel, otherwise same as unotice()
        """
        if self.quiet or not self.channel:
            return self.unotice(*args, **kwargs)
        return self.say(*args, **kwargs)

    def qnotice(self, *args, **kwargs):
        """
        Same as notice() if quiet is not set, otherwise same as unotice()
        """
        if self.quiet:
            return self.unotice(*args, **kwargs)
        return self.notice(*args, **kwargs)

    def qreply(self, *args, **kwargs):
        """
        Same as reply() if quiet is not set or there's no channel, otherwise same as unotice()
        """
        if self.quiet:
            return self.unotice(*args, **kwargs)
        return self.reply(*args, **kwargs)


def setup(filename, db_upgrade=True, supports_restart=False, times_restarted=None,  **kwargs):
    global bot, command, rule

    # asyncio.get_event_loop()  # Ensure it's created
    loop = tornado.platform.asyncio.AsyncIOMainLoop()
    loop.install()
    # asyncio.set_event_loop(loop.asyncio_loop)

    bot = ircbot.Bot(filename=filename, event_factory=Event)
    ircbot.modules.core.help_command.allow_full = False
    ircbot.modules.core.help_command.category = 'Core'
    bot.command_registry.register(ircbot.modules.core.help_command)
    command = bot.command
    rule = bot.rule
    bot.config.section('ratbot', RatbotConfig)

    # Attempt to determine some semblance of a version number.
    version = None
    try:
        if bot.config.ratbot.version_string:
            version = bot.config.ratbot.version_string
        elif bot.config.ratbot.version_file:
            with open(bot.config.ratbot.version_file, 'r') as f:
                version = f.readline().strip()
        else:
            import shlex
            import os.path
            import inspect
            import subprocess

            path = os.path.abspath(os.path.dirname(inspect.getframeinfo(inspect.currentframe()).filename))

            if bot.config.ratbot.version_cmd:
                cmd = bot.config.ratbot.version_cmd
            else:
                cmd = shlex.quote(bot.config.ratbot.version_git) + " describe --tags --long --always"
            output = subprocess.check_output(cmd, cwd=path, shell=True, universal_newlines=True)
            version = output.strip().split('\n')[0].strip()
    except Exception as ex:
        print("Failed to determine version: " + str(ex))
    if not version:
        version = '<unknown>'
    else:
        bot.realname += " " + version

    if times_restarted:
        logger.info(
            "Reincarnating Ratbot (times: {times_restarted}), running version {version}."
            .format(times_restarted=times_restarted, version=version)
        )
    else:
        logger.info(
            "Starting {restartable}Ratbot version {version}."
            .format(version=version, restartable="restartable " if supports_restart else '')
        )

    bot.data['ratbot'] = {
        'executor': concurrent.futures.ThreadPoolExecutor(max_workers=10),
        'version': version,
        'stats': {'started': datetime.datetime.now(tz=datetime.timezone.utc)},
        'restart_control': {
            'enabled': supports_restart,
            'count': times_restarted,
            'exit_status': None
        }
    }

    ratlib.db.setup(bot, upgrade=db_upgrade)
    ratlib.starsystem.refresh_bloom(bot)
    result = ratlib.starsystem.refresh_database(
        bot,
        callback=lambda: logger.info("EDSM database is out of date.  Starting background refresh."),
        background=True
    )
    if result:
        result.add_done_callback(lambda *unused: logger.info("Background EDSM refresh completed."))


def start():
    global pending
    for fn in pending:
        logger.info("Running " + fn.__module__ + "." + fn.__name__)
        fn(bot)
    pending = []
    try:
        bot.connect()
        bot.handle_forever()
    except Exception as ex:
        logger.exception("Encountered an unhandled exception during the main event loop.  Terminating.")
        sys.exit(1)
    sys.exit(bot.data['ratbot']['restart_control']['exit_status'] or 0)
