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
from ratbot import *
import ratlib.db

logger = logging.getLogger(__name__)


class AuthConfig(ircbot.ConfigSection):
    def read(self, section):
        self.admins = re.findall(r'[^\S,]+', section.get("admins", "").lower())
        self.cache_privileges = section.getbool('cache_privileges', True)
bot.config.section('auth', AuthConfig)


class AccountController:
    def __init__(self, bot):
        self.bot = bot

        self.nicks = {}  # Mapping of nicks to Accounts
        self.accounts = {}  # Mapping of account names to Accounts.
        # Note that nicks can be mapped to "anonymous" accounts without names.  If they are, there will not be
        # a corresponding entry under self.accounts.


class Account:
    """Represents an Account (possibly an unauthenticated user."""
    PLATFORMS = ('pc', 'xb')

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

        self.cached_privileges = None  # 'None' = not initialized/expired, set() = no permissions.

        if nicks:
            for nick in nicks:
                self.add_nick(nick)

        self.data = {}  # Misc data
        self.iam = {}  # Temporary IAm data

    def privileges(self):
        """Returns a set of the user's privileges."""
        if self.name is None:
            return set()
        if self.cached_privileges is None:
            db = ratlib.db.get_session(self.bot)
            query = db.query(
                ratlib.db.priv_inherits_table.c.

            )


            db = ratlib.db.get_session(self.bot)
            result = db.query(ratlib.db.Priv.)



    def can(self, priv):
        """
        Returns True if the user can perform the selected privilege.
        :param priv:
        :return:
        """



    def rename(self, name):
        """
        Change our account name.

        :param name: New name.
        """
        if name == self.name:  # noop
            return
        self.cached_privileges = None
        if self.controller:
            if name is not None:
                self.controller.accounts[name] = self
            if self.name is not None:
                del self.controller.accounts[self.name]
        self.name = name

    def add_nick(self, nick):
        """
        Adds a nickname to our set.

        :param nick: New nickname.
        """
        self.nicks.add(nick)
        if self.controller:
            self.controller.nicks[nick] = self

    def remove_nick(self, nick):
        """
        Removes a nickname from our set.  If this is the last nickname, removes us from the accounts dictionary.

        :param nick: Former nickname.
        """
        self.nicks.discard(nick)
        if self.controller:
            del self.controller.nicks[nick]
            if not self.nicks and self.name is not None:
                del self.controller.accounts[self.name]

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
            self.controller.nicks[new] = self
            del self.controller.nicks[old]
