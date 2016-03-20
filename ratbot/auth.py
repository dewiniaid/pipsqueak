"""
Handles authentication and authorization tasks.

There's a few pieces of this:
- An AccountController which listens to on_user_* events and creates/removes Account objects as needed.
- Accounts, which represent either an authenticated user (across all nicks) or an unauthenticated user (single nick)
- Privileges.

Privileges are defined on a per-account level, and look like this:
- An account can be granted any number of permissions, which are simple word strings.
- Each privilege contains a list of other privileges it implies.
- Privileges with @ are user-defined -- the bot doesn't use them internally, but they can be configured to assign
  groups of permissions.
"""
import logging
import ircbot
import re
import functools
from ratbot import *
from ratlib.db import get_session, with_session, models
from sqlalchemy import sql, orm

logger = logging.getLogger(__name__)
command = functools.partial(command, category='ADMIN')

#
#
# class NormalizedString(str):
#     """
#     Base class for str subclasses that have a 'normalized' form for comparisons.
#     """
#     def __new__(cls, *args, **kwargs):
#         instance = super().__new__(cls, *args, **kwargs)
#         instance._normalized = instance._normalize(instance)
#
#     @classmethod
#     def _normalize(cls, s):
#         """Returns the 'normalized' form of this string for comparison purposes."""
#         return s
#
#     def __hash__(self):
#         return self._normalized.__hash__()
#
# def _addmethod(attr):
#     compare = getattr(str, attr)
#     @functools.wraps(compare)
#     def method(self, other):
#         if type(other) == type(self):  # Same class as us, let us compare.
#             return compare(self._normalized, other._normalized)
#         if isinstance(other, type(self)):  # Subclass of us so is more specialized, let subclass compare.
#             return NotImplemented
#         if isinstance(other, str):
#             return compare(self._normalized, self._normalize(other))
#         return compare(self, other)
#     setattr(NormalizedString, attr, method)
#
# for _attr in ('__eq__', '__ne__', '__lt__', '__le__', '__gt__', '__ge__'):
#     _addmethod(_attr)
# del _attr
# del _addmethod
#
#
# class CaseInsensitiveString(NormalizedString):
#     def _normalize(cls, s):
#         return s.lower()


class AuthConfig(ircbot.ConfigSection):
    def read(self, section):
        self.admins = set(re.findall(r'[^\s,]+', section.get("admins", "").lower()))
        self.cache_privileges = section.getboolean('cache_privileges', True)
bot.config.section('auth', AuthConfig)


@prepare
def setup(bot):
    bot.data.setdefault('auth', {})
    bot.data['auth']['controller'] = AccountController(bot)


class AccountController:
    def __init__(self, bot):
        self.bot = bot
        self.nicks = {}  # Mapping of nicks to Accounts
        self.accounts = {}  # Mapping of account names to Accounts.
        # Note that nicks can be mapped to "anonymous" accounts without names.  If they are, there will not be
        # a corresponding entry under self.accounts.

        self.bot.on('user_create', fn=self.on_user_create)
        self.bot.on('user_delete', fn=self.on_user_delete)
        self.bot.on('user_update', fn=self.on_user_update)
        self.bot.on('user_rename', fn=self.on_user_rename)

    def _adduser(self, nick, name):
        lname = name.lower() if name else name
        logger.debug("_adduser: nick {!r}, account {!r}".format(nick, name))
        if lname is not None and lname in self.accounts:  # Authenticated user w/ existing account
            self.accounts[lname].add_nick(nick)
        else:
            Account(controller=self, name=name, nicks=[nick])  # Will register itself.

    def _deluser(self, nick):
        logger.debug("_deluser: nick {!r}".format(nick))
        self.nicks[nick.lower()].remove_nick(nick)

    def on_user_create(self, bot, user, nickname):
        """Event handler for when the bot 'creates' a user."""
        assert bot is self.bot
        if nickname.lower() in self.nicks:
            logger.warning("Attempted to add nickname '{}', which is already in our nick table.".format(nickname))
            return
        self._adduser(nickname, user.get('account'))

    def on_user_delete(self, bot, user, nickname):
        assert bot is self.bot
        if nickname.lower() not in self.nicks:
            logger.warning("Attempted to delete nickname '{}', which wasn't in our nick table.".format(nickname))
            return
        self._deluser(nickname)

    def on_user_update(self, bot, user, nickname):
        assert bot is self.bot
        if nickname.lower() not in self.nicks:
            logger.warning("Attempted to update nickname '{}', which wasn't in our nick table.".format(nickname))
            return
        name = user.get('account')
        account = self.nicks[nickname.lower()]
        if account.name != name:  # Account name mismatch.  Either lost or gained identity.
            self._deluser(nickname)
            self._adduser(nickname, name)  # Readd user with correct account

    def on_user_rename(self, bot, user, nickname, oldname):
        assert bot is self.bot
        if oldname.lower() not in self.nicks:
            logger.warning("Attempted to rename from nickname '{}', which wasn't in our nick table.".format(oldname))
            return
        if nickname.lower() in self.nicks:
            logger.warning("Attempted to rename to nickname '{}', which is already in our nick table.".format(nickname))
            return
        self.nicks[oldname.lower()].rename_nick(oldname, nickname)

    def expire_priv(self, priv):
        """
        Expire all account structures that have this privilege cached.  Used for modifications to groups.
        """
        if not self.bot.config.auth.cache_privileges:
            return
        for account in self.accounts.values():
            if account.cached_privs is not None and priv in account.cached_privs:
                account.cached_privs = None

    def expire_account(self, name):
        """
        Expire privileges on the specified account if we have it.
        """
        account = self.accounts.get(name.lower())
        if account:
            account.cached_privs = None

class Account:
    """Represents an Account (possibly an unauthenticated user."""
    PLATFORMS = ('pc', 'xb')
    name = None
    cached_privs = None  #: set of all privileges (including inherited).  None = not initialized.

    def __init__(self, controller=None, name=None, nicks=None, bot=None):
        """
        Creates a new account.

        :param controller: Owning controller.
        :param name: Account name.  None if anonymous.
        :param nicks: Set of associated nicks.
        :param bot: Bot instance.  Not required if we have a controller.
        """
        if not nicks:
            logger.error("Cannot initialize an account with no nicks.")
            
        self.nicks = set()
        self.controller = controller
        self.bot = bot
        if self.bot is None and self.controller:
            self.bot = self.controller.bot

        self.rename(name)  # We do this rather than setting our name because it will register us with the controller.

        if nicks:
            for nick in nicks:
                self.add_nick(nick)

        self.data = {}  # Misc data
        self.iam = {}  # Temporary IAm data

    def privileges(self):
        """Returns a set of the user's privileges."""
        if self.name is None:
            return set()
        if self.cached_privs is not None:
            return self.cached_privs
        with get_session(self.bot) as db:
            cte = (
                db.query(models.AccountPriv.priv_id)
                .join(models.Account)
                .filter(models.Account.name == self.name.lower())
                .cte(recursive=True)
            )
            alias = orm.aliased(cte)
            cte = cte.union(
                db.query(models.PrivInherits.child_id)
                .join(alias, models.PrivInherits.parent_id==alias.c.priv_id)
            )
            query = (
                db.query(models.Priv.name)
                .join(cte, cte.c.priv_id==models.Priv.id)
            )
            result = set(row.name for row in query)
            if '*' in result:
                # We did all of that for nothing?  (User has all the things.)
                result = set(row.name for row in db.query(models.Priv.name))
            if self.bot.config.auth.cache_privileges:
                self.cached_privs = result
        return result

    def is_admin(self):
        return self.name.lower() in self.bot.config.auth.admins

    def can(self, priv):
        """
        Returns True if the user can perform the selected privilege.
        :param priv:
        :return:
        """
        return self.name is not None and (self.is_admin() or priv in self.privileges())

    def rename(self, name):
        """
        Change our account name.

        :param name: New name.
        """
        if name == self.name:  # noop
            return
        self.cached_privs = None
        if self.controller:
            if name is not None:
                self.controller.accounts[name.lower()] = self
            if self.name is not None:
                del self.controller.accounts[self.name.lower()]
        self.name = name

    def add_nick(self, nick):
        """
        Adds a nickname to our set.

        :param nick: New nickname.
        """
        self.nicks.add(nick)
        if self.controller:
            self.controller.nicks[nick.lower()] = self

    def remove_nick(self, nick):
        """
        Removes a nickname from our set.  If this is the last nickname, removes us from the accounts dictionary.

        :param nick: Former nickname.
        """
        self.nicks.discard(nick)
        if self.controller:
            del self.controller.nicks[nick.lower()]
            if not self.nicks and self.name is not None:
                del self.controller.accounts[self.name.lower()]

    def rename_nick(self, old, new):
        """
        Updates our set of associated nicks to represent a nick change.

        :param old: Old nick
        :param new: New nick
        :return:
        """
        if old == new:
            return
        self.nicks.discard(old)
        self.nicks.add(new)
        if self.controller:
            self.controller.nicks[new.lower()] = self
            del self.controller.nicks[old.lower()]


def requires_account(priv=None, message="Permission denied.", kwarg='account', fn=None):
    """
    Wraps a function in such a way where it will receive an 'account' parameter containing the user's account.

    This passes an 'account' keyword argument to the wrapped function.  Altering the value of kwarg will change the
    name of the passed keyword argument.

    If `priv` is not-None, it represents a privilege the user must have.  If the user does not have that privilege, the
    wrapped function will not be called and the bot will reply with the contents of `message` instead.  If the user is
    not identified with Nickserv, the message will include a hint to do that.

    If `priv` is callable, it is assumed to be the value of `fn` and all other values are assumed to be their defaults.
    This allows this decorator to be used as `@requires_account` in addition to `@requires_account(...)`

    If `fn` is `None`, returns a decorator.

    Note that all decorated functions may potentially execute a WHOIS.

    :param priv: Privilege to check for.
    :param message: Message displayed on failed privilege check.
    :param kwarg: Name of keyword argument that stores the account name.
    :param fn: Function to wrap
    """
    if callable(priv):
        return requires_account(fn=priv)
    if fn is None:
        return functools.partial(requires_account, priv, message, kwarg)

    @functools.wraps(fn)
    @coroutine
    def wrapper(event, *args, **kwargs):
        if kwarg not in kwargs:
            controller = event.bot.data['auth']['controller']

            # We don't need the value ourselves, but this ensures they have one
            result = yield from event.bot.adapt_result(event.bot.get_user_value(event.nick, 'account'))
            account = controller.nicks.get(event.nick.lower())
            if account is None:
                logger.warning(
                    "Incoming event from nick '{}', which has no account table entry.  This will likely cause errors, "
                    " and should never happen.".format(event.nick)
                )
            kwargs[kwarg] = account
        else:
            account = kwargs[kwarg]
        if priv and not check_priv(event, account, priv, message):
            return
        return (yield from event.bot.adapt_result(fn(event, *args, **kwargs)))
    return wrapper


def check_priv(event, account, priv, message="Permission denied.", check_ident=True):
    """
    Returns True if the account has the privilege; sends the appropriate message and returns False otherwise,

    :param event: Event for sending messages.
    :param account: Account
    :param priv: Privilege
    :param message: Message to send if the privilege is denied.
    :param check_ident: If True and the user is not identified, append a hint to the error message.
    :return:
    """
    if account and account.can(priv):
        return True
    if check_ident and account and account.name is None:  # Anonymous.
        event.qreply(message + "  (HINT: You are not identified with NickServ.)")
    else:
        event.qreply(message)
    return False


@command('privs')
@bind('[?action=show]', 'Show your current privileges.')
@requires_account
async def cmd_privs(event, action=None, account=None):
    event.reply("Account {0!r} (name: {0.name!r}): {1!r}".format(account, account.privileges()))


# @command('grant')
# @bind(
#     'nametype=NICK/ACCOUNT/GROUP <name:str>',
#     "Show privileges granted to the specified nickname, account, or group."
# )
# @bind(
#     'nametype=NICK/ACCOUNT/GROUP <name:str> <+privs:str?privileges>',
#     "Grant the selected privileges to the specified nickname, account, or group."
# )
# @bind(
#     '<name:str>',
#     "Show privileges granted to the specified nickname, +account, or @group."
#     "  (Does not include inherited privileges.)"
# )
# @bind(
#     '<name:str> <+privs:str?privileges>',
#     "Grant the selected privileges to the specified nickname, +account, or @group"
# )
# @doc(
#     "Views or grants privileges to a given entity.  Privileges are internally stored per account, which are based on"
#     " NickServ identities, which can be viewed with /WHOIS <nick>.  By default, the <name> references a nick which must"
#     " be online and identified.  Prefixing the name with + means it refers to a specific account.  Prefixing the name"
#     " with @ means it refers to a group."
#     "\nGroups are privileges in themselves -- add and remove users from a group by granting or denying them the group's"
#     " permission.  (e.g. !grant Nickname @admins)."
#     "\nGranting the special privilege '*' implicitly grants all other privileges, including future additions."
#     " Revoking '*' does not remove all privileges, only those that are not explicitly granted by another grant."
#     " Revoking ALL removes all privileges from the target entity.  Revoking ALL from a group also removes the group."
#     "  (Simply removing all privileges from a group individually does not remove the group.)"
#     "\nYou cannot grant or revoke permissions that you do not yourself have."
# )


@requires_account
@with_session
@coroutine
def cmd_grant_revoke(event, nametype=None, name=None, privs=None, account=None, db=None, grant=True):
    def _ucfirst(s):
        """str.capitalize lowercases everything else, which we don't want."""
        if s[0].isupper():
            return s
        return s[0].upper() + s[1:]

    # For messaging
    verb = 'grant' if grant else 'revoke'
    fmtargs = {
        'verb': verb,
        'verbed': 'granted' if grant else 'revoke',
        'to': 'to' if grant else 'from'
    }

    controller = event.bot.data['auth']['controller']
    if nametype is None:
        if name[0] == '@':
            nametype = 'group'
            name = name[1:]
        elif name[0] == '+':
            nametype = 'account'
            name = name[1:]
        else:
            nametype = 'nick'
        if not name:
            event.qreply("A {nametype} name must be specified.".format(nametype))
            return

    if not check_priv(event, account, "auth."+verb):
        return

    is_group = nametype == 'group'
    if is_group:
        if not check_priv(event, account, "auth.groups", message="You are not authorized to modify group privileges."):
            return
        if name[0] != '@':
            name = '@' + name
        name = name.lower()
        fmtargs['tag'] = "group " + name
    elif nametype == 'nick':
        result = yield event.bot.whois(name)
        if not result:
            event.qreply("Nick '{}' does not appear to be online.".format(name))
            return
        if 'account' not in result:
            logger.warning(
                "WHOIS reply for '/WHOIS {}' did not include usable account information."
                "  Account information may not be fully supported on this server."
            )
            event.qreply("WHOIS reply did not include usable account information.  This is probably a bug.")
            return
        if not result['account']:
            event.qreply("Nickname '{}' is not identified with NickServ.".format(name))
            return
        fmtargs['tag'] = "account {} (online as {})".format(result['account'], name)
        name = result['account']
    else:
        fmtargs['tag'] = "account {}".format(name)
    del nametype
    lname = name.lower()
    if not is_group and lname in event.bot.config.auth.admins:
        fmtargs['tag'] += " (ADMIN)"


    # Retrieve the target account/group
    is_new = False

    # Naming scheme:
    # privs = requested changes
    # existing = existing permissions
    # unchanged = permissions that won't change status.  (Already granted privs, or nonexistant privs during revoke)
    # changed = permissions that change status.
    if is_group:
        try:
            target = db.query(models.Priv).filter(models.Priv.name == lname).one()
        except orm.exc.NoResultFound:
            target = models.Priv(name=lname)
            is_new = True
        existing = set(target.child_names)
        target_collection = target.children
    else:
        try:
            target = db.query(models.Account).filter(models.Account.name == lname).one()
        except orm.exc.NoResultFound:
            target = models.Account(name=lname)
            is_new = True
        existing = set(target.priv_names)
        target_collection = target.privs
    changed = []
    unchanged = existing

    if privs:
        # We're granting/revoking privileges
        if not is_group and lname == account.name.lower() and not account.is_admin():
            event.qreply("Only admins can {verb} privileges {to} themselves.".format(**fmtargs))
            return
        privs = set(priv.lower() for priv in privs)

        revoke_all = 'all' in privs
        if revoke_all:
            if grant:
                event.qreply("Cannot explicitly {verb} all privileges.  (Try '*' instead of 'all'?)".format(**fmtargs))
            # Handle some fast exit cases.
            if is_new:
                event.qreply(_ucfirst("{tag} does not exist, so there is nothing to do.".format(**fmtargs)))
            privs = set(existing)

        # Validate the list of privileges
        known_privs = {priv.name: priv for priv in db.query(models.Priv).filter(models.Priv.name.in_(privs))}
        missing_privs = privs - set(known_privs)
        if missing_privs:
            event.qreply(
                "Unknown privilege{s}: {privs}"
                .format(s='' if len(missing_privs) == 1 else 's', privs=", ".join(sorted(missing_privs)))
            )
            return
        if not account.is_admin():
            our_privs = account.privileges()
            denied_privs = privs - set(our_privs)
            if denied_privs:
                event.qreply(
                    "Permission denied because you lack the following privilege{s}: {privs}"
                    .format(s='' if len(denied_privs) == 1 else 's', privs=", ".join(sorted(denied_privs)))
                )
                return

        # Invalidate caches
        if is_group:
            controller.expire_priv(lname)
        else:
            controller.expire_account(lname)

        # Handle revoke_all.
        if revoke_all:
            if is_group:
                db.delete(target)
                db.commit()
                event.qreply("Deleted {tag}.".format(**fmtargs))
            if not privs:  # No privileges defined, meaning this is either a noop or it deletes a group
                event.qreply("{tag} has no privileges.".format(**fmtargs))
                return
            # Rest will be handled normally.

        # Assign privileges
        if grant:
            unchanged = existing & privs
            changed = privs - unchanged
            target_collection.update(known_privs.values())
        else:
            changed = privs & existing
            unchanged = privs - changed
            target_collection.difference_update(known_privs.values())

        # Generate messaging.
        if not unchanged:
            message = " ".join(("{verbed} {changed} to", "new" if is_new else "", "{tag}."))
        elif not changed:  # Nothing to do
            if grant:
                message = "".join((
                    "{tag} already has ",
                    "that privilege" if len(unchanged)==1 else "all of those privileges",
                    ": {unchanged}"
                ))
            else:
                message = "".join((
                    "{tag} does not have",
                    "that privilege" if len(unchanged)==1 else "any of those privileges",
                    ": {unchanged}"
                ))
        else:
            if grant:
                message = "{verbed} {changed} {to} {tag}.  {tag} already had {existing}."
            else:
                message = "{verbed} {changed} {to} {tag}.  {tag} did not have {existing}."

        db.add(target)
        db.commit()
    else:  # Reporting assigned messages!
        if is_new:
            message = "{tag} has no assigned privileges because they aren't in the account database."
        elif not existing:
            message = "{tag} has no assigned privileges."
        else:
            message = "{tag} is granted {existing}."
    changed = ", ".join(sorted(changed))
    unchanged = ", ".join(sorted(unchanged))
    existing = ", ".join(sorted(existing))
    message = message.format(changed=changed, unchanged=unchanged, existing=existing, **fmtargs)

    if not message[0].isupper():
        message = message[0].upper() + message[1:]
    event.qreply(message)


grant_revoke_doc = (
    "Privileges are internally stored per account, which are based on NickServ identities and can be viewed with"
    " /WHOIS <nick>.  By default, <name> references a nick which must be online and identified.  Prefixing the name"
    " with + (or using the ACCOUNT version of this command) means it refers to a specific account.  Prefixing the name"
    " with @ (or using the GROUP version of this command) means it refers to a group."
    "\nGroups are privileges in themselves -- add and remove users from a group by granting or revoking the group name"
    " (e.g. !grant Nickname @admins).  The group itself must exist before it can be granted or revoked."
    "\nGranting the special privilege '*' implicitly grants all other privileges, including future additions."
    " Revoking '*' does not remove all privileges, only those that are not explicitly granted by another grant."
    " Revoking ALL removes all privileges from the target entity.  Revoking ALL from a group also removes the group."
    "  (Simply removing all privileges from a group individually does not remove the group.)"
    "\nYou cannot grant or revoke permissions that you do not yourself have."
)
from_chain(
    functools.partial(cmd_grant_revoke, grant=True),
    command('grant'),
    bind(
        'nametype=NICK/ACCOUNT/GROUP <name:str>',
        "Show privileges granted to the specified nickname, account, or group."
        "  (Does not include inherited privileges.)"
    ),
    bind(
        'nametype=NICK/ACCOUNT/GROUP <name:str> <+privs:str?privileges>',
        "Grant the selected privileges to the specified nickname, account, or group."
    ),
    bind(
        '<name:str>',
        "Show privileges granted to the specified nickname, +account, or @group."
        "  (Does not include inherited privileges.)"
    ),
    bind(
        '<name:str> <+privs:str?privileges>',
        "Grant the selected privileges to the specified nickname, +account, or @group"
    ),
    doc("Views or grants privileges to a given entity.  " + grant_revoke_doc)
)


from_chain(
    functools.partial(cmd_grant_revoke, grant=False),
    command('revoke', aliases=['deny']),
    bind(
        'nametype=NICK/ACCOUNT/GROUP <name:str>',
        "Show privileges granted to the specified nickname, account, or group."
        "  (Does not include inherited privileges.)"
    ),
    bind(
        'nametype=NICK/ACCOUNT/GROUP <name:str> <+privs:str?privileges>',
        "Revoke the selected privileges from the specified nickname, account, or group."
    ),
    bind(
        '<name:str>',
        "Show privileges granted to the specified nickname, +account, or @group."
        "  (Does not include inherited privileges.)"
    ),
    bind(
        '<name:str> <+privs:str?privileges>',
        "Revoke the selected privileges from the specified nickname, +account, or @group"
    ),
    doc("Views or revokes privileges from a given entity.  " + grant_revoke_doc)
)
