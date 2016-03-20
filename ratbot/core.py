import datetime
import logging
from ratbot import *
import pydle.client
from . import auth

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


@command('shutdown', category='ADMIN')
@bind('[?confirm=confirm]', 'Tells the bot to shutdown.')
@auth.requires_account('shutdown.shutdown')
def cmd_shutdown(event, confirm=None, account=None):
    logger.info("<{event.nick}> {event.message}".format(event=event))
    if not confirm:
        event.reply("Use '{} confirm' if you are sure you wish to trigger shutdown.".format(event.full_name))
        return
    if event.data['ratbot']['restart_control']['exit_status'] is not None:
        event.reply('A shutdown or restart is already in progress.')
        return
    event.reply("Initiating shutdown sequence.")
    event.bot.data['ratbot']['restart_control']['exit_status'] = 0
    event.bot.quit("Shutdown requested by {}".format(event.nick))


@command('restart', category='ADMIN')
@bind('[?confirm=confirm]', 'Tells the bot to restart.')
@auth.requires_account('shutdown.restart')
def cmd_shutdown(event, confirm=None, account=None):
    logger.info("<{event.nick}> {event.message}".format(event=event))
    if not confirm:
        event.reply("Use '{} confirm' if you are sure you wish to trigger a restart.".format(event.full_name))
        return
    if event.data['ratbot']['restart_control']['exit_status'] is not None:
        event.reply('A shutdown or restart is already in progress.')
        return
    event.reply("Initiating restart sequence.")
    event.bot.data['ratbot']['restart_control']['exit_status'] = 100
    event.bot.quit("Restart requested by {}".format(event.nick))
#
#
# # Temporary
# @command('whois')
# @bind('<+nicknames:str>', 'Performs a WHOIS on the specified nicknames and returns a result.')
# @coroutine
# def cmd_whois(event, nicknames):
#     result = yield bot.whois(nicknames)
#     print(repr(result))
#     event.usay(repr(result).replace("\n", "  "))
#
#
# @command('uprop')
# @bind('<nickname:str> <property:str>')
# @coroutine
# def cmd_uprop(event, nickname, property):
#     result = yield bot.get_user_value(nickname, property)
#     print(repr(result))
#     event.usay(repr(result).replace("\n", "  "))
#
#
# @command('udump')
# @bind('<?user>')
# def cmd_udump(event, user=None):
#     import pprint
#     if user:
#         pprint.pprint(event.bot.users.get(user))
#     else:
#         pprint.pprint(event.bot.users)
#         pprint.pprint(event.bot.channels)


@command('join', category='ADMIN')
@bind('<channel> [<?password>]', 'Joins the specified channel, optionally using the specified password.')
@auth.requires_account('channel.join')
def cmd_join(event, channel, password=None, account=None):
    if not event.bot.is_channel(channel):
        event.qreply("'{}' doesn't look like a channel.".format(channel))
        return
    try:
        event.bot.join(channel, password)
    except pydle.client.AlreadyInChannel:
        event.qreply("I'm already in that channel.")


@command('part', category='ADMIN')
@bind('[<?channel>]', 'Parts (leaves) the specified channel. If no channel is specified, leaves the current channel.')
@auth.requires_account('channel.part')
def cmd_part(event, channel=None, account=None):
    if channel is None and event.channel is None:
        event.qreply("A channel must be specified.")
        return
    channel = channel or event.channel
    if not event.bot.is_channel(channel):
        event.qreply("'{}' doesn't look like a channel.".format(channel))
        return
    try:
        event.bot.part(channel)
    except pydle.client.NotInChannel:
        event.qreply("I'm not in that channel.")
