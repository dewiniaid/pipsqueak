import datetime
import concurrent.futures
import ircbot
from ircbot.commands.commands import Pattern, from_chain, bind, alias, doc
import ircbot.modules.core
import ratlib
import ratlib.db
import ratlib.starsystem
import logging
from pydle import coroutine


logger = logging.getLogger(__name__)


__all__ = [
    'bot', 'command', 'rule', 'RatbotConfig', 'setup', 'start', 'prepare',
    'Pattern', 'from_chain', 'bind', 'alias', 'doc'
]
bot = None
rule = None
command = None
pending = []

def prepare(fn):
    logger.debug("Preparing " + fn.__module__ + "." + fn.__name__)
    pending.append(fn)
    return fn


class RatbotConfig(ircbot.ConfigSection):
    def read(self, section):
        self.apiurl = section.get('apiurl', None)
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


def setup(filename):
    global bot, command, rule
    bot = ircbot.Bot(filename=filename, event_factory=Event)
    ircbot.modules.core.help_command.allow_full = False
    ircbot.modules.core.help_command.category = 'Core'
    bot.command_registry.register(ircbot.modules.core.help_command)
    command = bot.command
    rule = bot.rule
    bot.config.section('ratbot', RatbotConfig)

    @command('version')
    @alias('uptime')
    @bind('', 'Shows bot current version and running time.')
    def cmd_version(event):
        from ratlib import format_timedelta, format_timestamp
        started = bot.data['ratbot']['stats']['started']
        event.say(
            "Version {version}, up {delta} since {time}"
            .format(
                version=bot.data['ratbot']['version'],
                delta=format_timedelta(datetime.datetime.now(tz=started.tzinfo) - started),
                time=format_timestamp(started)
            )
        )

    # Temporary
    @command('whois')
    @bind('<+nicknames:str>', 'Performs a WHOIS on the specified nicknames and returns a result.')
    @coroutine
    def cmd_whois(event, nicknames):
        result = yield bot.whois(nicknames)
        print(repr(result))
        event.usay(repr(result).replace("\n", "  "))

    @command('uprop')
    @bind('<nickname:str> <property:str>')
    @coroutine
    def cmd_uprop(event, nickname, property):
        result = yield bot.get_user_value(nickname, property)
        print(repr(result))
        event.usay(repr(result).replace("\n", "  "))

    @command('udump')
    @bind('')
    def cmd_udump(event):
        import pprint
        pprint.pprint(event.bot.users)
        pprint.pprint(event.bot.channels)

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

    logger.info("Starting Ratbot version " + version)

    bot.data['ratbot'] = {
        'executor': concurrent.futures.ThreadPoolExecutor(max_workers=10),
        'version': version,
        'stats': {'started': datetime.datetime.now(tz=datetime.timezone.utc)}
    }

    ratlib.db.setup(bot)
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
    bot.connect()
    bot.handle_forever()
