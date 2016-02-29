import datetime
import concurrent.futures
import ircbot
from ircbot.commands import alias, match, bind, doc
import ratlib
import ratlib.db
import ratlib.starsystem

# __all__ = ['alias', 'match', 'bind', 'doc', 'bot', 'command', 'rule']
event = None
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


def setup(filename):
    global event, command, rule
    event = ircbot.Bot(filename=filename)
    command = event.command
    rule = event.rule
    event.config.section('ratbot', RatbotConfig)

    # Attempt to determine some semblance of a version number.
    version = None
    try:
        if event.config.ratbot.version_string:
            version = event.config.ratbot.version_string
        elif event.config.ratbot.version_file:
            with open(event.config.ratbot.version_file, 'r') as f:
                version = f.readline().strip()
        else:
            import shlex
            import os.path
            import inspect
            import subprocess

            path = os.path.abspath(os.path.dirname(inspect.getframeinfo(inspect.currentframe()).filename))

            if event.config.ratbot.version_cmd:
                cmd = event.config.ratbot.version_cmd
            else:
                cmd = shlex.quote(event.config.ratbot.version_git or 'git') + " describe --tags --long --always"
            output = subprocess.check_output(cmd, cwd=path, shell=True, universal_newlines=True)
            version = output.strip().split('\n')[0].strip()
    except Exception as ex:
        print("Failed to determine version: " + str(ex))
    if not version:
        version = '<unknown>'

    print("Starting Ratbot version " + version)

    event.data['ratbot'] = {
        'executor': concurrent.futures.ThreadPoolExecutor(max_workers=10),
        'version': version,
        'stats': {'started': datetime.datetime.now(tz=datetime.timezone.utc)}
    }

    ratlib.db.setup(event)
    ratlib.starsystem.refresh_bloom(event)
    ratlib.starsystem.refresh_database(
        event,
        callback=lambda: print("EDSM database is out of date.  Starting background refresh."),
        background=True
    )


def start():
    event.connect()
    event.handle_forever()
