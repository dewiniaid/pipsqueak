"""
Manages and recites stored facts.

Copyright 2015, Dimitri "Tyrope" Molenaars <tyrope@tyrope.nl>
Copyright 2016, Daniel "Dewin" Grace
Licensed under the Eiffel Forum License 2.
"""
import json
import os.path
import re
import glob

from sqlalchemy import exc, inspect
import ircbot

from ratbot import *
from ratlib.db import Fact, with_session


class FactConfig(ircbot.ConfigSection):
    def read(self, section):
        self.filename = section.get('filename')
        langs = re.findall(r'[^\S,]+', section.get("lang", "en").lower())
        if not langs:
            langs = ['en']
        self.langs = langs

bot.config.section('ratfacts', FactConfig)


@with_session
def import_facts(bot, merge=False, db=None):
    """
    Import json data into the fact database

    :param bot: IRC bot
    :param merge: If True, incoming facts overwrite existing ones rather than being ignored.
    """
    filename = bot.config.ratfacts.filename
    if not filename:
        return
    lang = bot.config.langs[0]

    def _gen(json):
        for k, v in json.items():
            if isinstance(v, dict):
                for name, message in v.items():
                    if isinstance(message, dict):  # New-style facts.json with attribution
                        yield Fact(name=name, lang=k, message=message['fact'], author=message.get('author'))
                    else:   # Newer-style facts.json with language but not attribution -- or explicit deletion of fact.
                        yield Fact(name=name, lang=k, message=message)
            else:  # Old-style facts.json, single language
                yield Fact(name=k, lang=lang, message=v)

    for fact in _gen(load_fact_json(filename)):
        try:
            with db.begin_nested():
                if merge:
                    fact = db.merge(fact)
                    if fact.message is None:
                        if fact in db.new:
                            db.expunge(fact)
                        else:
                            db.delete(fact)
                else:
                    if fact.message is None:
                        continue
                    db.add(fact)
                db.flush()
        except exc.DatabaseError:
            if merge:  # Shouldn't have errors in this case
                raise
    db.commit()


def load_fact_json(path, recurse=True):
    """
    Loads facts from the specified filename.

    If filename is a directory and recurse is True, loads all json files in that directory.
    """
    facts = {}
    if recurse and os.path.isdir(path):
        for filename in glob.iglob(os.path.join(path, "*.json")):
            result = load_fact_json(filename, recurse=False)
            if result:
                for k, v in result.items():
                    if isinstance(v, dict) and isinstance(facts.get(k), dict):
                        facts[k].update(v)
                    else:
                        facts[k] = v
        return facts

    with open(path, encoding='utf-8-sig') as f:
        print(path)
        try:
            facts = json.load(f)
        except Exception as ex:
            print("Failed to import file {!r}".format(path))
            raise

    if not isinstance(facts, dict):
        # Something horribly wrong with the json
        raise RuntimeError("{}: json structure is not a dict.".format(path))
    return facts


@with_session
def find_fact(bot, text, exact=False, db=None):
    lang_search = bot.config.ratfacts.langs

    fact = Fact.find(db, name=text, lang=lang_search[0] if exact else lang_search)
    if fact:
        return fact
    if '-' in text:
        name, lang = text.rsplit('-', 1)
        if not exact:
            lang = [lang] + lang_search
        return Fact.find(db, name=name, lang=lang)
    return None


def format_fact(fact):
    return (
        "\x02{fact.name}-{fact.lang}\x02 - {fact.message} ({author})"
        .format(fact=fact, author=("by " + fact.author) if fact.author else 'unknown')
    )


@command(aliases=[Pattern(lambda x: True, key='recite_fact', doc='<fact>', priority=1000)])
# High priority value so we're last.
@bind('<?rats:line>')
def cmd_recite_fact(event, rats=None):
    """Recite facts"""
    fact = find_fact(event.bot, event.name)

    if not fact:
        return

    if rats:
        # Reorganize the rat list for consistent & proper separation
        # Split whitespace, comma, colon and semicolon (all common IRC multinick separators) then rejoin with commas
        rats = ", ".join(filter(None, re.split(r"[,\s+]", rats))) or None
        return event.reply(fact.message, reply_to=rats)
    return event.qsay(fact.message)


@command('fact', aliases=['facts'])
@bind('', "Lists all known facts.")
@bind('?full=FULL', "Lists all known facts and their definitions in all languages.  Very spammy.")
@bind(
    '<key:str?fact> [?full=FULL]',
    "Shows detailed stats on the specified fact.  FULL: Display all translations of this fact."
)
@bind(  # This technically will never match, it's just here for documentation.
    '<key:str?lang> [?full=FULL]',
    "Shows detailed stats on the specified language.  FULL: Display all facts in this language."
)
@bind('action=import?IMPORT [?force=-f]', "Reimports JSON data.  -f overwrites existing data.")
@bind(
    'action=ADD/SET <factid:str:lower?fact-lang> <text:line>',
    "Creates or updates the specified fact in the specified language."
)
@bind(
    'action=DEL/DELETE/REMOVE/RM <factid:str:lower?fact-lang>',
    "Removes the specified fact from the specified language."
)
@bind(
    '<factid:str:lower?fact-lang> action=IS <text:line>',
    "Creates or updates the specified fact, or removes it if the text is the word 'nothing'.  Alternate usage."
)
@doc(
    "Languages are added simply by adding a fact in that language.  The convention is to use the 2-letter language"
    " code as listed here: https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes"
    "\n"
    "\n<fact-lang> means a fact name, followed by a hyphen, followed by a language code.  Example: \"name-en\""
)
@with_session
def cmd_fact(event, action='show', key=None, factid=None, text=None, force=False, full=False, db=None):
    """
    Lists known facts, list details on a fact, or rescans the fact database.
    """
    action = action.lower()
    action = {'set': 'add', 'delete': 'del', 'remove': 'del', 'rm': 'del'}.get(action, action)

    if factid:
        # If factid is set, we're using a command that requires it.  Validate it.
        name, _, lang = factid.rpartition('-')
        if not name:
            raise FinalUsageError("The first portion of a fact id cannot be empty.")
        if not lang:
            raise FinalUsageError(
                "Fact must include a language specifier.  (Perhaps you meant '{}-{}'?)"
                .format(name, event.bot.config.ratfacts.langs[0])
            )

        if action == 'is':
            if text.lower() == 'nothing':
                action = 'del'
            else:
                action = 'add'

        if action == 'add':
            name = db.merge(Fact(name=name, lang=lang, message=text, author=event.nick))
            is_new = not inspect(name).persistent
            db.commit()
            event.qsay(("Added " if is_new else "Updated ") + format_fact(name))
            return

        if action == 'del':
            name = Fact.find(db, name=name, lang=lang)
            if name:
                db.delete(name)
                db.commit()
                event.qsay("Deleted " + format_fact(name))
            else:
                event.qsay("No such fact '{}'".format(factid))
            return

        raise RuntimeError('Reached code that should be unreachable.')

    # Still here.  Must be one of the other commands
    if action == 'import':
        # if access & (HALFOP | OP):
        import_facts(event.bot, merge=bool(force))
        event.qsay("Facts imported.")
        return
        # return bot.reply("Not authorized.")

    if action != 'show':
        raise RuntimeError('Reached code that should be unreachable.')

    if not key:
        if full:
            # if not (access & (HALFOP | OP)):
            #     return bot.reply("Not authorized.")
            if event.channel and not event.quiet:
                event.reply("Messaging you the complete fact database.")
            event.usay("Language search order is {}".format(", ".join(event.bot.config.ratfacts.langs)))
            for fact in Fact.findall(db):
                event.usay(format_fact(fact))
            event.usay("-- End of list --")
            return
        # List known facts.
        facts = list(Fact.unique_names(db))
        if not facts:
            event.qsay("Like Jon Snow, I know nothing.  (Or there's a problem with the fact database.)")
            return
        langs = list(Fact.unique_langs(db))
        event.qsay(
            "{numfacts} known fact(s) ({facts}) in {numlangs} language(s) ({langs})"
            .format(numfacts=len(facts), facts=", ".join(facts), numlangs=len(langs), langs=", ".join(langs))
        )
        return

    def _translation_stats(exists, missing, s='translation', p='translations'):
        msg = []
        if exists:
            msg.append("{count} {word} ({names})".format(
                count=len(exists), word=s if len(exists) == 1 else p, names=", ".join(sorted(exists))
            ))
        else:
            msg.append("no " + p)
        if missing:
            msg.append("missing {count} ({names})".format(count=len(missing), names=", ".join(sorted(missing))))
        else:
            msg.append("none missing")
        return ", ".join(msg)

    for attr, opposite, name, opposite_name_s, opposite_name_p in [
        ('name', 'lang', 'fact', 'translation', 'translations'),
        ('lang', 'name', 'language', 'fact', 'facts')
    ]:
        if not db.query(Fact.query(db, order_by=False, **{attr: command}).exists()).scalar():
            continue
        sq = Fact.unique_query(db, field=getattr(Fact, opposite), order_by=False).subquery()
        sq_opp = getattr(sq.c, opposite)
        fact_opp = getattr(Fact, opposite)
        fact_col = getattr(Fact, attr)

        query = (
            db.query(Fact, sq_opp)
            .select_from(sq.outerjoin(Fact, (sq_opp == fact_opp) & (fact_col == command)))
            .order_by(Fact.message.is_(None), sq_opp)
        )
        exists = set()
        missing = set()
        if full:
            if event.channel and not event.quiet:
                event.reply("Messaging you what I know about {} '{}'".format(name, command))
            event.usay("Fact search for {} '{}'".format(name, command))
        for name, key in query:
            if name and full:
                event.usay(format_fact(name))
            (exists if name else missing).add(key)

        summary = (
            "{} '{}': ".format(name.title(), key) +
            _translation_stats(exists, missing, s=opposite_name_s, p=opposite_name_p)
        )
        if full:
            event.usay(summary)
        else:
            event.qsay(summary)
        return

    event.qsay("'{}' is not a known fact, language, or subcommand".format(key))
    return
