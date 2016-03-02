import datetime
import concurrent.futures
import ircbot
from ircbot.commands import alias, match, bind, doc
import ratlib
import ratlib.db
import ratlib.starsystem

__all__ = ['alias', 'match', 'bind', 'doc', 'bot', 'command', 'rule', 'RatbotConfig', 'setup', 'start']
bot = None
rule = None
command = None


class RatbotConfig(ircbot.ConfigSection):
    def read(self, section):
        self.apiurl = section.get('apiurl', None)
        self.alembic = section.get('alembic', 'alembic.ini')
        self.debug_sql = section.getboolean('debug_sql', False)
        self.quiet_command = section.get('quiet_command', '@')

        self.database = section['database']

        # TODO: Make these their own config
        self.edsm_url = section.get('edsm_url', "http://edsm.net/api-v1/systems?coords=1")
        self.edsm_maxage = section.getint('edsm_maxage', 12*60*60)
        self.edsm_autorefresh = section.getint('edsm_autorefresh', 4*60*60)

        self.version_string = section.get('version_string')
        self.version_file = section.get('version_file')
        self.version_cmd = section.get('version_cmd')
        self.version_git = section.get('version_git', 'git')


def setup(filename):
    global bot, command, rule
    bot = ircbot.Bot(filename=filename)
    ircbot.add_help_command(bot)
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

    print("Starting Ratbot version " + version)

    bot.data['ratbot'] = {
        'executor': concurrent.futures.ThreadPoolExecutor(max_workers=10),
        'version': version,
        'stats': {'started': datetime.datetime.now(tz=datetime.timezone.utc)}
    }

    ratlib.db.setup(bot)
    ratlib.starsystem.refresh_bloom(bot)
    ratlib.starsystem.refresh_database(
        bot,
        callback=lambda: print("EDSM database is out of date.  Starting background refresh."),
        background=True
    )


def start():
    bot.connect()
    bot.handle_forever()
