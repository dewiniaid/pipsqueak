import logging
import logging.handlers
import sys
import argparse
import re
import subprocess
import time

logger = logging.getLogger(__name__)


class LogRecord(logging.LogRecord):
    """Extends the normal LogRecord with an 'altlevelname' for alternate level names."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.altlevelname = self._level_remap.get(self.levelname, self.levelname)

    _level_remap = {
        'WARNING': 'WARN',
        'CRITICAL': 'FATAL'
    }


def get_full_command_line():
    """
    Attempts to return what is (most) of the original Python command line.
    """
    flag_opt_map = {
        'debug': 'd',
        'optimize': 'O',
        'dont_write_bytecode': 'B',
        'no_user_site': 's',
        'no_site': 'S',
        'ignore_environment': 'E',
        'verbose': 'v',
        'bytes_warning': 'b',
        'quiet': 'q',
        'hash_randomization': 'R',
    }
    args = [sys.executable]
    for flag, opt in flag_opt_map.items():
        v = getattr(sys.flags, flag)
        if v > 0:
            if flag == 'hash_randomization':
                v = 1 # Handle specification of an exact seed
            args.append('-' + opt * v)
    for opt in sys.warnoptions:
        args.append('-W' + opt)
    args.extend(sys.argv)
    return args


def watchdog():
    cmdline = get_full_command_line()
    cmdline.extend(['--times-restarted', '<blank>'])
    restarts = -1  # Will be 0 momentarily.

    # Threshold to avoid crazy respawn madness.
    failed_runs = 0
    sleep_times = [0, 1, 2, 2, 30, 60, 300, 1800]
    last_fail = 0
    fail_timeout = 300  # 5 minutes without crashing resets the respawn timer

    while True:
        restarts += 1
        cmdline[-1] = str(restarts)
        result = subprocess.run(cmdline, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
        if result.returncode == 0:
            print("Child process exited normally.  Not restarting.", file=sys.stderr)
            return 0  # Normal exit.
        if result.returncode == 100:  # Our special 'intentional requesting restart' code.
            print("Child process requested a restart.  Restarting in 5 seconds...", file=sys.stderr)
            time.sleep(5)
            continue
        now = time.time()
        if now - last_fail > fail_timeout:
            failed_runs = 1
        else:
            failed_runs += 1
        last_fail = now
        if failed_runs >= len(sleep_times):
            print("Fail #{} since last timeout.  Too many fails.  Bailing.".format(failed_runs), file=sys.stderr)
            return 1
        print(
            "Fail #{} since last timeout.  Sleeping {} seconds.".format(failed_runs, sleep_times[failed_runs - 1]),
            file=sys.stderr
        )
        time.sleep(sleep_times[failed_runs - 1])


def main():
    def parse_log_freq(value):
        value = value.trim().lower()

        # Known intervals.  Note the logging interface doesn't support weeks, but we can multiply by 7 easy enough.
        intervals = dict()
        for it in (
            ((k, 's') for k in ('s', 'sec', 'secs', 'second', 'seconds')),
            ((k, 'm') for k in ('m', 'min', 'mins', 'minute', 'minutes')),
            ((k, 'h') for k in ('h', 'hr', 'hrs', 'hour', 'hours')),
            ((k, 'd') for k in ('d', 'day', 'days', '')),
            ((k, 'w') for k in ('w', 'wk', 'wks', 'week', 'weeks'))
        ):
            intervals.update(it)

        # Known "special" intervals.
        special_intervals = dict(('midnight', k) for k in ('daily', 'midnight'))
        for ix, day in enumerate(('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')):
            key = "W" + str(ix)
            special_intervals.update((key, value) for value in (key, day, day[:3]))

        if value in special_intervals:
            return special_intervals[value], 1

        result = re.match(r'(\d+)\s*([a-z]*)', value)
        if not result:
            raise argparse.ArgumentTypeError("Invalid frequency '{}'.".format(value))
        result = (result.group(2), int(result.group(1)))
        if result[1] < 1:
            raise argparse.ArgumentTypeError("Log rotation interval cannot be less than 1.")
        if result[0] not in intervals:
            raise argparse.ArgumentTypeError("Unknown log rotation frequency '{}'.".format(result[0]))
        return result

    # Set up argument parser.
    parser = argparse.ArgumentParser(usage="Starts Pipsqueak")
    parser.add_argument(
        '-r', '--restartable', action='store_true', dest='restartable',
        help="Allows the client to restart itself."
    )
    parser.add_argument(
        '--times-restarted', action='store', dest='times_restarted', type=int,
        help="Used internally by --restartable and should not be specified directly."
    )
    parser.add_argument(
        '-l', '--log-file', action='store', dest='log_file', metavar='FILE',
        help="Write log output to LOGFILE."
    )
    parser.add_argument(
        '-L', '--log-level', action='store', dest='log_level', default='INFO', metavar='LEVEL',
        help=(
            "Set default logging level to LEVEL, which may be a number or one of"
            " {critical,fatal,error,warning,warn,info,debug}"
        )
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        '-e', '--log-stderr', action='store_const', const=True, dest='log_stderr',
        help="Write logs to STDERR.  Implied if no LOGFILE is specified."
    )
    group.add_argument(
        '-E', '--no-log-stderr', action='store_const', const=False, dest='log_stderr',
        help="Don't write logs to stderr, even if no logfile is specified."
    )
    parser.add_argument(
        '--log-rotate', action='store_true', dest='log_rotate',
        help=(
            "Whether to periodically rotate logs.  Specifying any of the other log-rotate-* options automatically"
            " turns this on.  Specifying this by itself sets some reasonable defaults as a shortcut."
        )
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        '--log-rotate-freq', action='store', dest='log_rotate_freq', metavar='FREQ', type=parse_log_freq,
        help=(
            'Rotate logs every FREQ, where FREQ is a number followed by one of (S,M,H,D) indicating seconds, minutes,'
            ' hours, or days, respectively.  Cannot be combined with  --log-rotate-size'
        )
    )
    group.add_argument(
        '--log-rotate-size', action='store', dest='log_rotate_size', metavar='BYTES', type=int,
        help='Rotate logs when the file size reaches BYTES.  Cannot be combined with --log-rotate-freq'
    )
    parser.add_argument(
        '--log-rotate-count', action='store', dest='log_rotate_count', metavar='COUNT',
        help='Maintain at most COUNT previous logs (0=unlimited).'
    )
    parser.add_argument(
        '--db-upgrade', action='store_true', dest='db_upgrade', default=True,
        help='Checks for required database upgrades on startup.  This is the default.'
    )
    parser.add_argument(
        '--skip-db-upgrade', action='store_false', dest='db_upgrade', default=True,
        help='Skips checking for required database upgrades on startup.  May cause errors if an upgrade is needed!'
    )
    parser.add_argument(
        '-c', action='store_true', dest='obsolete_options',
        help='Obsolete option to specify a configuration file.  Ignored.'
    )
    parser.add_argument(
        action='store', dest='config', nargs='?', default='ratbot.cfg',
        metavar='FILE', help="Configuration file."
    )

    # Parse and validate options.
    opts = parser.parse_args()
    if opts.log_rotate_freq is not None or opts.log_rotate_size is not None:
        opts.log_rotate = True
    elif opts.log_rotate or opts.log_rotate_count is not None:
        opts.log_rotate = True
        opts.log_rotate_freq = ('midnight', 1)
        if opts.log_rotate_count is None:
            opts.log_rotate_count = 7
    if opts.log_rotate:
        if opts.log_file is None:
            parser.error("Log rotation options require a log file.")
        if opts.log_file == '-':
            parser.error("Log rotation options cannot be used when using stdout as the logfile.")
    if opts.log_level is None:
        opts.log_level = logging.INFO
    else:
        try:
            level = int(opts.log_level)
        except ValueError:  # Invalid integer
            level = logging.getLevelName(opts.log_level.upper())  # Broken in 3.4 and 3.4.1, but we require 3.5 anyways.
            if not isinstance(level, int):  # getLevelName does weird things when passed a nonexistant level.
                parser.error("Unknown log level '{}'.".format(opts.log_level))
        opts.log_level = level
    if opts.log_stderr is None:
        opts.log_stderr = not opts.log_file

    # Make sure the configuration file is readable.
    try:
        open(opts.config, 'r').close()
    except Exception as ex:
        parser.error("Unable to read configuration file: " + str(ex))

    # Make sure the log file is readable.
    if opts.log_file is not None and opts.log_level != '-':
        try:
            open(opts.log_file, 'a').close()
        except Exception as ex:
            parser.error("Unable to append to log file: " + str(ex))

    # Handle restart control.
    if opts.restartable and opts.times_restarted is None:
        return watchdog()

    # Set up logging
    handlers = []
    if opts.log_stderr:
        handlers.append(logging.StreamHandler(sys.stderr))
    if opts.log_file == '-':
        handlers.append(logging.StreamHandler(sys.stdout))
    elif opts.log_file is not None:
        if opts.log_rotate_freq:
            handlers.append(logging.handlers.TimedRotatingFileHandler(
                opts.log_file, *opts.log_rotate_freq, backupCount=opts.log_rotate_count or 0
            ))
        elif opts.log_rotate_size:
            handlers.append(logging.handlers.RotatingFileHandler(
                opts.log_file, maxBytes=opts.log_rotate_size, backupCount=opts.log_rotate_count or 0
            ))
        else:
            handlers.append(logging.FileHandler(opts.log_file))

    logging.setLogRecordFactory(LogRecord)
    if handlers:
        logging.basicConfig(
            level=opts.log_level,
            handlers=handlers,
            datefmt='%Y-%m-%d %H:%M:%S',
            format="{asctime} {altlevelname:5} [{name}] {message}",
            style='{'
        )
        logger.debug("Logging configured.")

    import ratbot
    ratbot.setup(
        opts.config,
        db_upgrade=opts.db_upgrade,
        supports_restart=opts.times_restarted is not None,
        restart_count=opts.times_restarted
    )

    import ratbot.core
    import ratbot.auth
    import ratbot.autocorrect
    import ratbot.search
    import ratbot.facts
    import ratbot.board
    # import ratbot.drill

    ratbot.start()


if __name__ == '__main__':
    sys.exit(main())
