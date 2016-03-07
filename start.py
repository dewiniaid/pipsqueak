if __name__ == '__main__':
    import ratbot
    import sys

    filename = sys.argv[1] if len(sys.argv) > 1 else 'ratbot.cfg'
    ratbot.setup(filename)

    import ratbot.autocorrect
    import ratbot.search
    import ratbot.facts
    # import ratbot.board
    # import ratbot.drill

    ratbot.start()
