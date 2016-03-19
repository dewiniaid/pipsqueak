import datetime
import logging
from ratbot import *

logger = logging.getLogger(__name__)


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


@command('shutdown')
@bind('[?confirm=confirm]', 'Tells the bot to shutdown.')
def cmd_shutdown(event, confirm=None):
    logger.info("<{event.nick}> {event.message}".format(event=event))
    if not confirm:
        event.reply("Use '{} confirm' if you are sure you wish to trigger shutdown.".format(event.full_name))
        return
    if event.data['ratbot']['restart_control']['exit_status'] is not None:
        event.reply('A shutdown or restart is already in progress.')
        return
    event.reply("Initiating shutdown sequence.")
    event.data['ratbot']['restart_control']['exit_status'] = 0
    event.disconnect()


@command('restart')
@bind('[?confirm=confirm]', 'Tells the bot to restart.')
def cmd_shutdown(event, confirm=None):
    logger.info("<{event.nick}> {event.message}".format(event=event))
    if not confirm:
        event.reply("Use '{} confirm' if you are sure you wish to trigger a restart.".format(event.full_name))
        return
    if event.data['ratbot']['restart_control']['exit_status'] is not None:
        event.reply('A shutdown or restart is already in progress.')
        return
    event.reply("Initiating restart sequence.")
    event.data['ratbot']['restart_control']['exit_status'] = 100
    event.disconnect()


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
