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
from ratbot import *
import ratlib.db
import logging
logger = logging.getLogger(__name__)


class AccountController:
    def __init__(self, bot):
        self.bot = bot

        self.nicks = {}  # Mapping of nicks to Accounts
        self.accounts = {}  # Mapping of account names to Accounts.

        # Note that nicks can be mapped to "anonymous" accounts without names.  If they are, there will not be
        # a corresponding entry under self.accounts.


class Account:
    """Represents an Account (possibly an unauthenticated user."""
    def __init__(self, controller, name=None, nicks=None):
        """
        Creates a new account.

        :param controller: Owning controller.
        :param name: Account name.  None if anonymous.
        :param nicks: Set of associated nicks.
        """
        if not nicks:
            logger.error("Cannot initialize an account with no nicks.")
            
        self.nicks = set()
        self.controller = controller
        self.rename(name)  # We do this rather than setting our name because it will register us with the controller.

        if nicks:
            for nick in nicks:
                self.add_nick(nick)

        self.data = {}

    def rename(self, name):
        """
        Change our account name.

        :param name: New name.
        """
        if name == self.name:  # noop
            return
        if name is not None:
            self.controller.accounts[name] = self
        if self.name is not None:
            self.controller.accounts[self.name]
        self.name = name

    def add_nick(self, nick):
        """
        Adds a nickname to our set.

        :param nick: New nickname.
        """
        self.nicks.add(nick)
        self.controller.nicks[nick] = self

    def remove_nick(self, nick):
        """
        Removes a nickname from our set.  If this is the last nickname, removes us from the accounts dictionary.

        :param nick: Former nickname.
        """
        self.nicks.discard(nick)
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
        self.controller.nicks[new] = self
        del self.controller.nicks[old]


