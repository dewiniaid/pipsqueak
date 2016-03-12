
if __name__ == '__main__':
    import logging

    class LogRecord(logging.LogRecord):
        """Extends the normal LogRecord with an 'altlevelname' for alternate level names."""
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.altlevelname = self._level_remap.get(self.levelname, self.levelname)

        _level_remap = {
            'WARNING': 'WARN',
            'CRITICAL': 'FATAL'
        }

    logging.setLogRecordFactory(LogRecord)
    logging.basicConfig(
        level=logging.DEBUG,
        datefmt='%Y-%m-%d %H:%M:%S',
        format="{asctime} {altlevelname:5} [{name}] {message}",
        style='{'
    )


    import ratbot
    import sys

    filename = sys.argv[1] if len(sys.argv) > 1 else 'ratbot.cfg'
    ratbot.setup(filename)

    import ratbot.autocorrect
    import ratbot.search
    import ratbot.facts
    import ratbot.board
    # import ratbot.drill

    ratbot.start()
