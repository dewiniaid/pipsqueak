"""
IRC command parsing tools.

This module defines several classes and utility functions related to defining IRC bot commands and parsing arguments
for them.

Argument Parsing
================
Argument parsing works on the notion of splitting a string of space-separated words, but with an easy way to take any
word and retrieve the remainder of the string that was originally parsed.

The :class:`ArgumentList` class parses a line of text and converts it into a list of :class:`Argument`s.  Arguments
are subclasses of :class:`str` that add some extra fluff -- namely, the :attr:`~Argument.eol` property which returns
from the beginning of the selected argument up through the end of the line that was parsed.  This is notably useful
for commands that take a few 'word' arguments followed by a partial line of text.

Command Binding
===============
Command binding is the process of taking a particular set of arguments (in a :class:`ArgumentList`), interpreting them,
and calling a function with said arguments.

The definition of a particular :class:`CommandBinding` is written in a simple syntax that resembles the same sort of
text you might write as a one-line "Usage: " instruction, e.g. something like::

   CommandBinding("set <key> <value>")

Which will call a function using function(key=word, value=word)

The :class:`CommandBinding` interface is designed in such a way where a command ultimately can have multiple unique
bindings for various subcommands.

Commands
========
The most basic :class:`Command` decorates a function in such a way that it is called with the following signature::

    function(bot, event)

If a :class:`Command` has one or more bindings associated, the functions will instead be called as::

    function(bot, event, parameters_based_on_bindings

Bindings will be tried against the function and input parameters in such a way where the first binding that 'fits' is
the one that will be called.
"""


import re
import inspect
import collections
import functools


class Argument(str):
    """
    Like a str, but with some added attributes useful in command parsing.

    Normally, Arguments should not be constructed directly but instead created in bulk from an ArgumentList.
    """
    def __init__(self, s, text, span):
        """
        Constructs a new Argument

        :param s: String that we will be set to.
        :param text: Full line of text involved in the original parse.
        :param span: Tuple of (start, end) representing where we were located in the original parse.
        """
        self._text = text
        self._start = span[0]
        self._eol = None
        super.__init__(s)

    @property
    def eol(self):
        """
        Returns the original string from the beginning of this Argument to the end of the line.

        Calculated on first access.
        """
        if self._eol is None:
            self._eol = self.text[self._start:]
            del self._text
            del self._start
        return self._eol


class ArgumentList(list):
    """
    When parsing IRC commands, it's often useful to have the input text split into words -- usually by
    re.split(r'\s+', ...) or similar.

    This allows for that, while also allowing for a way to get the remainder of the line as one solid chunk,
    unaltered by any spaces.
    """
    pattern = re.compile(r'\S+')

    def __init__(self, text):
        """
        Parse a string of text (likely said by someone on IRC) into a series of words.
        :param text: Original text.
        """
        self.text = text
        super().__init(
            Argument(match.group(), self.text, match.start())
            for match in self.pattern.finditer(self.text)
        )


class ParseError(ValueError):
    """
    Represents an error in parsing :class:`CommandBinding` syntax.

    :ivar message: Error message.
    :ivar text: Text being parsed.
    :ivar pos: Position of error, if known.
    """
    def __init__(self, message=None, text=None, pos=None):
        self.message = message
        self.text = text
        self.pos = pos
        super().__init__(message)

    def __str__(self):
        msg = [self.message or 'Parse error']
        if self.pos:
            msg += [' at position ' + str(self.pos)]
        return "".join(msg)

    def __repr__(self):
        values = [self.message or 'Parse error', self.text, self.pos]
        while values and values[-1] is None:
            values.pop()
        return self.__class__.__name__ + repr(tuple(values))

    def show(self, maxwidth=None, after=10):
        """
        Pretty-prints out a version of the actual parse error: showing a (portion) of the text and an arrow pointing to
        the portion that failed.

        :param maxwidth: Maximum line width.  0 or none=no limit.  Must be >= 0
        :param after: If a line is trimmed to maxwidth, ensure at least this many characters after 'pos' are visible
            (assuming there's at least that many characters left in the string).  Must be >= 0

        If either of self.text or self.pos are None, this simply returns str(self).

        If the values of 'after' and 'maxwidth' are too close, 'after' is reduced to make room for at least 4 leading
        characters ("..." + the character that triggered the exception).
        :return: Formatted multiline string.
        """
        if maxwidth and maxwidth < 0:
            raise ValueError('maxwidth cannot be negative')
        if after and after < 0:
            raise ValueError('after cannot be negative')

        if self.text is None or self.pos is None:
            return str(self)
        # Replace newlines with spaces.  We shouldn't really have newlines anyways.
        text = str.replace(self.text, "\n", " ")

        # Format string
        fmt = "{text}\n{arrow}^\n{desc}"

        if maxwidth:
            diff = (maxwidth + 4) - after
            if diff < 0:
                after = min(0, after + diff)
            end = min(len(text), self.pos + after + 1)
            start = max(0, end - maxwidth)
            if start:
                text = "..." + text[start+3:end]
            else:
                text = text[:end]
            pos = self.pos - start
        else:
            pos = self.pos
        return "{text}\n{arrow}^\n{desc}".format(
            text=text, arrow='-'*pos, desc=str(self)
        )


class UsageError(Exception):
    """
    Thrown when a command is called with invalid syntax.
    """
    def __init__(self, message=None, args=None, param=None):
        """
        Creates a new UsageError.

        UsageErrors represent instances where a user enters invalid syntax for an IRC command.  For instance, if they
        fail to specify the correct number of parameters to a command, or enter an integer where a string is expected.

        There are two types of UsageErrors: normal and final.

        A command can have multiple bindings to represent different subcommands.  This works by trying the bindings,
        in order, until one does not raise a UsageError.  This means that the binding in question accepted the
        specified commands as intended for it.

        FinalUsageError is thrown when a binding thinks it was most likely intended for a command, but the parameters
        were still wrong.

        :param message: Error message.
        :param args: The ArgumentList that was being parsed.
        :param param: The Parameter that triggered the error.
        """


class FinalUsageError(UsageError):
    """See UsageError"""
    pass


class CommandBinding:
    """
    A given IRC command can have one or more bindings.
    A binding consists of:
        - A function to call
        - A priority (default 0)
        - A parameter string, consisting of a a space-separated list of parameters.

    If the first word of a line of text matches one of the command names, the remainder is checked against the bindings
    available for that command in order of priority.  (Bindings with the same priority are called in the order they
    were defined).

    The first binding that is matched calls its function with arguments set corresponding to contents of the
    parameter string.

    Parameters in the parameter string can consist of a mix of constants and variables:

    Constants:
    Constants are optionally wrapped in parenthesis.  They consist of the following, in order:
    - An optional argument name, followed by an equals sign, which will receive a lowercased version of the constant.
    - One or more words, separated by the | or / characters.  Text has to be a (case-insensitive) match to one of the
      listed words.
    - An optional helpname, consisting of a question mark (?) followed by text that should be used in place of the
      variable name in help.  This may contain spaces as long as the constant is wrapped in parenthesis.

    Examples:

    foo - Word must exactly match 'foo'
    action=add|delete - Word must be 'add' or 'delete', and the function's 'action' argument will match the word.
    (action=add|delete?ACTION) - As above, but will show as ACTION in helptext.


    When a constant is present, parsing expects an exact (case-insensitive) match to one of the words listed:
    foo
        Word must be 'foo'
    foo|bar or foo/bar
        Word must be 'foo' or 'bar'

    A constant may be optionally preceded with "name=".  If so, the variable named 'name' will be set to the constant
    value when calling the function.

    Variables:
    Variables are wrapped in angle brackets and normally correspond 1-to-1 with words in the text being processed.
    They consist of the following, in order:
    - A name, which must match the name of a function argument (unless the function has **kwargs)
    - An optional type specifier, consisting of a colon (:) plus the name of a type.  Currently supported types are:
        :line - Matches the rest of the line instead of the usual one word.  Should be the last parameter.
        :str - Input coerced to string.  This is the default.
        :int - Input coerced to integer.
    - An optional helpname, consisting of a question mark (?) followed by text that should be used in place of the
      variable name in help.  This may contain spaces.

    The variable name may be followed by a '*' or a '+' to indicate that it should receive the remaining arguments as a
    list.  If this is the case, it must be the final parameter.  If the variable name is the same as the name of the
    *args parameter in the function, '*' is implied if neither option is specified.  '*' means it must have 0 or more
    arguments, '+' is 1 or more.

    Optional parameters:
    Each chunk may optionally begin with any number of ['s and end with any number of ]'s to indicate that they are
    optional.  This only affects the help text.  To make a particular parameter actually optional, the function being
    called need only specify a default value for it.

    :ivar label: Our label
    :ivar function: The function that we're bound to.
    :ivar signature: A `class:inspect.Signature` from the bound function
    :ivar usage: Usage helptext generated by parameters.
    :ivar params: Parameters.
    :ivar summary: Short summary of usage.
    """
    _paramstring_symbols = '[^<>()]'

    # Each group is named with 'prefix_name[_unused]
    # Prefix is used for classification.  Name is used for variable naming.  Unused exists solely as a way to avoid
    # duplicate parameter names.
    _paramstring_re = re.compile(
        r'''
        # Search for an actual parameter or somesuch
        (?P<prefix_0>\[*)                   # Allow any number of brackets to indicate optional sections
        (?:
        # Parameter
        (?:(?P<prefix_1>\<)                 # begin
            (?P<var_arg>%N+?)               # argument name
            (?:\:(?P<var_type>%N+?))?       # optional type specifier
            (?:\:(?P<var_options>%N+?))?    # optional type specifier
            (?:\?(?P<var_name>%N+?))?       # optional helptext
        (?P<suffix_1>\>))                   # end
        | # Or constant
        (?:(?P<prefix_2>\()                 # begin
            (?:(?P<const_arg>%N+?)=)?       # optional argument name
            (?P<const_options>%N+?)         # constant phrase
            (?:\?(?P<const_name>%N+?))?     # optional helptext
        (?P<suffix_2>\)))                   # end
        | # Or a constant w/o parenthesis
        (?:
            (?:(?P<const_arg_1>%N+?)=)?     # optional argument name
            (?P<const_value_1>%N+?)         # constant phrase
            (?:\?(?P<const_name_1>%N+?))?   # helptext
        )
        | # Anything else is invalid
        (?P<error_unexpected>.+?)
        )
        (?P<suffix_3>\]*)                   # Allow any number of brackets to indicate optional sections
        (?:\s+|\Z)                          # Some whitespace or the end of the string
        '''.replace('%N', _paramstring_symbols), re.VERBOSE
    )

    default_type = 'str'
    type_registry = {}

    def __init__(self, function, paramstring, label=None, summary=None):
        """
        Creates a new :class:`CommandBinding`.

        :param function: The function that we bind.
        :param paramstring: The parameter string to interpret.
        :param label: Name of this binding.  Optional.
        :param summary: Optional short summary of usage.
        """
        self.label = label
        self.function = function
        self.signature = inspect.signature(function)
        self.summary = summary
        signature = self.signature

        kwargs_var = None
        varargs_var = None
        for data in signature.parameters.values():
            if data.kind == inspect.Parameter.VAR_KEYWORD:
                kwargs_var = data.name
            elif data.kind == inspect.Parameter.VAR_POSITIONAL:
                varargs_var = data.name

        def parse_error_here(message):
            return ParseError(message, paramstring, match.start())

        def adapt_parse_error(ex):
            if ex.message is not None or ex.pos is not None:
                raise ex
            else:
                raise parse_error_here(ex.message)
            raise ex

        params = []          # List of parameter structures
        self.params = params
        arg_names = set()    # Found parameter names (to avoid duplication)
        usage = []      # Usage line.  (Starts as a list, combined to a string later.)
        eol = False          # True after we've consumed a parameter that eats the remainder of the line.

        for index, match in enumerate(self._paramstring_re.finditer(paramstring)):
            if eol:
                raise parse_error_here(
                    "Previous parameter consumes remainder of line, cannot have additional parameters."
                )
            # Parse our funky regex settings
            data = collections.defaultdict(dict)
            for key, value in match.groupdict.items():
                if value is None:
                    continue
                keytype, key, *unused = key.split("_")
                data[keytype][key] = value
            prefix = "".join(v for k, v in sorted(data.pop('prefix', {}).items(), key=lambda x: match.group(x[0])))
            suffix = "".join(v for k, v in sorted(data.pop('suffix', {}).items(), key=lambda x: match.group(x[0])))
            assert len(data) == 1, "Unexpectedly matched multiple sections of paramstring."

            paramtype, data = data.popitem()  # Should only have one key left.
            if paramtype == 'error':
                error_type = next(iter(data.values()))
                raise parse_error_here({'unexpected': "Unexpected characters"}.get(error_type, error_type))

            # paramtype will be one of 'var' or 'const'
            # data will consist of:
            # arg (default to None)
            # type ('const' if paramtype == 'str', else default to 'str')
            # options (default to None)
            # name (default to arg)
            arg = data.get('arg') or None
            options = data.get('options')
            name = data.get('name')
            listmode = Parameter.LIST_NONE
            required = True

            if paramtype == 'const':
                type_ = 'const'
                name = name or options
            else:
                type_ = data.get('type') or self.default_type
                name = name or arg

            if arg:
                if arg[-1] in '+*':
                    listmode = Parameter.LIST_NORMAL  # we might override this in a moment that's fine.
                    required = (arg[-1] == '+')
                    arg = arg[:-1]
                if arg in arg_names:
                    raise parse_error_here("Duplicate parameter name '{!r}'".format(arg))
                if arg == varargs_var:
                    listmode = Parameter.LIST_VARARGS
                if arg == kwargs_var:
                    raise parse_error_here("Cannot directly reference a function's kwargs argument.")
                if arg not in signature.parameters:
                    if not kwargs_var:
                        raise parse_error_here("Bound function has no argument named '{!r}'".format(arg))
                    required = False
                elif not listmode:
                    required = (signature.parameters[arg].default is inspect.Parameter.empty)
                arg_names.add(arg)
            try:
                param = Parameter(self, index, arg, type_, options, name, listmode, required)
                eol = listmode or param.eol
                params.append(param)
            except ParseError as ex:
                raise adapt_parse_error(ex)
            usage.append(prefix + name + ("..." if listmode else "") + suffix)

        self.usage = " ".join(usage

    @classmethod
    def register_type(cls, class_, typename=None, *args, **kwargs):
        """
        Registers a subclass of :class:`ParamType` as a type handler to match cases of <param:typename>

        :param class_: :class:`ParamType` subclass that will handle this type.  If None, returns a decorator.  If this
            is not a callable, it is treated as the value of 'typename' instead and a decorator is returned.
        :param typename: Name of the type
        :param args: Passed to _class's constructor after 'options'.
        :param kwargs: Passed to _class's constructor after 'options'.
        """
        if class_ is not None and not callable(class_):
            args = [typename] + list(args)
            typename = class_
            class_ = None

        def decorator(class_):
            if typename in cls.type_registry:
                raise ValueError("Type handler {!r} is already registered".format(typename))
            cls.type_registry[typename] = (class_, args, kwargs)
            return class_

        return decorator if class_ is None else decorator(class_)

    def bind(self, arglist, *args, **kwargs):
        """
        Binds the information in input_string
        :param arglist: An :class:`ArgumentList` cosisting of the arguments we wish to bind.
        :param args: Initial arguments to include in binding.
        :param kwargs: Initial keyword arguments to include in binding.
        :return: Outcome of signature.Bind()
        """
        for param in self.params:
            param.bind(arglist, args, kwargs)
        return self.signature.bind(*args, **kwargs)

    def __call__(self, arglist, *args, **kwargs):
        """
        Calls the bound function.

        Equivalent to ``bound = binding.bind(...); binding.function(*bound.args, **bound.kwargs))``

        :param arglist: An :class:`ArgumentList` cosisting of the arguments we wish to bind.
        :param args: Initial arguments to include in binding.
        :param kwargs: Initial keyword arguments to include in binding.
        :return: Result of function call.
        """
        bound = self.bind(arglist, *args, **kwargs)
        return self.function(*bound.args, **bound.kwargs)


class Parameter:
    LIST_NONE = 0
    LIST_NORMAL = 1
    LIST_VARARGS = 2
    def __init__(self, parent, index, arg, type_=None, options=None, name=None, listmode=LIST_NONE, required=True):
        """
        Defines a new parameter

        :param parent: Parent :class:`CommandBinding`.
        :param index: Index in list
        :param arg: Function argument name.  May be None in some circumstances.
        :param type_: Parameter type
        :param options: Parameter type options.
        :param name: Helpname.  Defaults to 'arg' if not set.
        :param listmode: LIST_NONE(default) if this is a single argument.  LIST_NORMAL if this is a list of arguments.
            LIST_VARARGS if this is a function's *args
        :param required: True if this parameter is required.  For lists, this means it must match 1+ items instead of
            0+ items.
        """
        if name is None:
            name = arg

        self.index = index
        self.parent = parent
        self.arg = arg
        self.type_ = type_
        self.options = None
        self.name = name
        self.listmode = listmode
        self.required = required

        parser_class, args, kwargs = self.parent.type_registry[self.type_]
        self.parser = parser_class(self, *args, **kwargs)

    def args(self, arglist):
        """
        Yields arguments from arglist that we are directly responsible for.  Helper function.

        :param arglist: A :class:`ArgumentList`
        """
        index = self.index
        while index < len(arglist):
            yield index
            if not self.listmode:
                return

    def validate(self, arglist):
        """
        Performs validation of arguments in :class:`ArgumentList` that belong to us.

        Gives ParamType handlers a chance to raise a UsageError() before actually invoking the function involved.

        :param arglist: A :class:`ArgumentList`
        """
        if len(arglist) >= self.index:
            if self.required:
                if self.listmode:
                    raise UsageError("At least one {name} must be specified".format(name=self.name or '<const>'))
                raise UsageError("{name} must be specified".format(name=self.name or '<const>'))
            return
        if not self.parser.check:
            return
        for arg in self.args(arglist):
            try:
                self.parser.validate(arg.eol if self.parser.eol else arg)
            except UsageError:
                raise
            except Exception as ex:
                if self.parser.wrap_exceptions:
                    raise UsageError("Invalid format for {name}") from ex
                raise

    def bind(self, arglist, args, kwargs):
        """
        Updates the args and kwargs that we'll use to call the bound function based on what the ParamType handler says

        :param arglist: A :class:`ArgumentList`
        """
        values = []
        for arg in self.args(arglist):
            try:
                values.append(self.parser.parse(arg.eol if self.parser.eol else arg))
            except UsageError:
                raise
            except Exception as ex:
                if self.parser.wrap_exceptions:
                    raise UsageError("Invalid format for {name}") from ex
                raise
        if not self.arg:
            # If we don't have an argument name, we don't want to update anything.  However, it's still necessary to
            # do all of the above work in case it would throw a UsageError.
            return
        if not values:
            return
        if not self.listmode:
            kwargs[self.arg] = values[0]
            return
        if self.listmode == self.LIST_VARARGS:
            args.extend(values)
            return
        kwargs[self.arg] = values


class ParamType:
    """
    Defines logic and rules behind argument parsing.

    There's three passes to argument parsing:
    1. At bind time, an ArgumentType() instance is created.  It receives any options specified as its first argument.

    2. Before calling a function, any arguments that have 'check=True' have their validate method called.  If any of
       them raise the function will not be called and the system will continue to the next binding (if one exists)

    3. Before calling a function, all arguments have their parse() method called, and the result is assigned to one of
       the function arguments.  If any of these raise, the function will not be called. and the system will continue
       to the next binding (if one exists)

    :ivar eol: If True, this argument type consumes the entire line instead of just one word.
    :ivar wrap_exceptions: If True, all non-UsageError exceptions from cls.parse() are re-raised as UsageErrors.
    :ivar check: If True, call validate() before parse().  Use this when the parsing may have side effects or
        would be otherwise expensive, but simple validation is easy.
    :ivar param: Options passed by the CommandBinding process.  Not currently implemented.
    """
    eol = False
    wrap_exceptions = True
    check = False

    def __init__(self, param):
        """
        Created as part of the CommandBinding process.  Should raise a ParseError if invalid.

        :param param: Parameter we are attached to.
        """
        self.param = param

    def parse(self, value):
        """
        Parses the incoming string and returns the parsed result.

        :param value: Value to parse.
        :return: Parsed result.
        """
        return self.value

    def validate(self, value):
        """
        Preparses the incoming string and raises an exception if it fails early validation.

        :param value: Value to parse.
        :return: Nothing
        """
        return


@CommandBinding.register_type('str')
@CommandBinding.register_type('line', eol=True)
class StrParamType(ParamType):
    def __init__(self, param, eol=False):
        """
        String arguments are the simplest arguments, and usually return their input.

        :param param: Parameter we are bound to.

        If param.options is equal to 'lower' or 'upper', the corresponding method is called on the string before it is
        returned.
        """
        super().__init__(param)
        self.eol = eol
        if param.options and param.options in ('lower', 'upper'):
            self.parse = getattr(str, param.options)
        else:
            self.parse = str


@CommandBinding.register_type('const')
class ConstParamType(ParamType):
    check = True

    def __init__(self, param, split=re.compile('[/|]').split):
        """
        Const arguments require that an argument be a case-insensitive exact match for one of the provided inputs.

        :param param: Parameter are bound to.
        :param split: Function that splits constant value string into an iterable of allowed values.

        param.options dictates allowed constant values, as a string
        """
        super().__init__(param)
        self.values = set(split(param.options.lower()))
        if not self.values:
            raise ParseError("Must have at least one constant value.")

    def validate(self, value):
        if value not in self.values:
            if len(self.values) == 1:
                fmt = "{name} must equal {values}"
            else:
                fmt = "{name} must be one of ({values})"
            raise UsageError(fmt.format(name=self.param.name, values=", ".join(self.values)))


@CommandBinding.register_type('int', coerce=int, coerce_error="{name} must be an integer")
@CommandBinding.register_type('float', coerce=float, coerce_error="{name} must be a number")
class NumberParamType(ParamType):
    def __init__(self, param, coerce=int, coerce_error=None):
        """
        Number arguments require that their data be a number, potentially within a set range.

        :param param: Parameter are bound to.
        :param coerce: Function that coerces 'min', 'max' and the input to integers.

        param.options may contain a string in the form of 'min..max' or 'min' that determines the lower and upper
        bounds for this argument.  if either 'min' or 'max' are blank (as opposed to 0), the range is treated as
        unbound at that end.
        """
        self.coerce = coerce
        self.coerce_error = coerce_error

        minvalue, _, maxvalue = param.options.partition('..')
        try:
            self.minvalue = coerce(minvalue) if minvalue else None
        except Exception as ex:
            raise ParseError("Unable to coerce minval using {!r}".format(coerce))

        try:
            self.maxvalue = coerce(maxvalue) if maxvalue else None
        except Exception as ex:
            raise ParseError("Unable to coerce maxval using {!r}".format(coerce))

        if self.minvalue is not None and self.maxvalue is not None and self.minvalue > self.maxvalue:
            raise ParseError("minval > maxval")

    def parse(self, value):
        try:
            value = self.coerce(value)
        except Exception as ex:
            if self.coerce_error:
                raise UsageError(self.coerce_error.format(name=self.param.name)) from ex
            raise

        if self.minvalue is not None and value < self.minvalue:
            if self.maxvalue is not None:
                raise UsageError(
                    "{0.param.name} must be between {0.minvalue} and {0.maxvalue}"
                    .format(self)
                )
            raise UsageError(
                "{0.param.name} must be >= {0.minvalue}"
                .format(self)
            )
        if self.maxvalue is not None and value > self.maxvalue:
            raise UsageError(
                "{0.param.name} must be <= {0.minvalue}"
                .format(self)
            )


class Command:
    """
    Represents commands.
    """

    name = None      # Command name for !help
    aliases = []     # Aliases.  (Case-insensitive string matching)
    patterns = []    # Patterns.  (Regular expressions or strings that will be compiled into one)
    bindings = []    # Associated command bindings, in order of priority.

    def __init__(self, name=None, aliases=None, patterns=None, bindings=None, help=None):
        """
        Defines a new command.

        Generally, you shouldn't instantiate this directly -- use the decorators instead.

        :param name: Command name, used in helptext.  If None, uses the first alias, or the first pattern.
        :param aliases: Command aliases.
        :param patterns: Regular expression patterns.
        :param bindings: Command bindings.
        :param help: Detailed help text.
        """
        self.name = name
        self.aliases = aliases or []
        self.patterns = patterns or []
        self.bindings = bindings or []
        self.help = help
        self._done = False
        self.pending_functions = []  # From things being added by decorators.

    def finish(self):
        """
        Called by decorators when the command is fully assembled.
        """
        if self._done:
            return
        for fn in reversed(self.pending_functions):
            fn()
        self.pending_functions = []

        for ix, pattern in self.patterns:
            if hasattr(pattern, 'pattern'):  # Compiled regex.
                continue
            self.patterns[ix] = re.compile(pattern, re.IGNORECASE)
        if not self.name:
            if self.aliases:
                self.name = self.aliases[0]
            elif self.patterns:
                self.name = self.patterns[0]
                if hasattr(self.name, 'pattern'):
                    self.name = self.name.pattern


def wrap_decorator(fn):
    """
    Returns a version of the function wrapped in such a way as to allow both decorator and non-decorator syntax.

    If the first argument of the wrapped function is a callable, the wrapped function is called as-is.

    Otherwise, returns a decorator
    """
    # Determine the name of the first argument, in case it is specified in kwargs instead.
    signature = inspect.signature(fn)
    arg = None
    param = next(iter(signature.parameters.items()), None)
    if param and param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
        arg = param.name

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if (args and callable(args[0])) or (arg in kwargs and callable(kwargs[arg])):
            return fn(*args, **kwargs)
        def decorator(_fn):
            return fn(_fn, *args, **kwargs)
        return decorator
    return wrapper

@wrap_decorator
def command(fn=None, name=None, aliases=None, patterns=None, bindings=None, help=None):
    """
    Command decorator.

    This must be the 'last' decorator in the chain of command construction (and thus, the first to appear when stacking
    multiple decorators)

    :param fn: Function to decorate, or a :class:`Command` instance.
    :param name: Command name.
    :param aliases: List of command aliases
    :param patterns: Regex patterns.
    :param bindings: Bindings.
    :param help: Helptext.
    :return: A decorator if fn is None, a finished Command otherwise.
    """
    if not isinstance(fn, Command):
        if not (name or aliases or patterns):
            name = fn.__name__
        if not bindings:
            bindings = [CommandBinding(fn, "")]
        fn = Command(name, aliases, patterns, bindings, help)
        fn.finish()

    if name:
        fn.name = name
    if aliases:
        fn.aliases = aliases + fn.aliases
    if patterns:
        fn.patterns = patterns + fn.patterns
    if bindings:
        fn.patterns = bindings + fn.bindings
    if help:
        if fn.help:
            fn.help = help + "\n" + fn.help
        else:
            fn.help = help
    fn.finish()

