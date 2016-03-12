#coding: utf8
"""
autocorrect.py - System name autocorrection.
Copyright 2016, Daniel "dewin" Grace

Licensed under the Eiffel Forum License 2.
!
This is currently a very rudimentary implementation, lacking any sort of configuration.
"""
from ratbot import *
import ratlib.autocorrect

def match_system(text):
    result = ratlib.autocorrect.correct(text)
    if not result.fixed:
        return False
    return result


@rule(match_system, priority=-1000)
def correct_system(event):
    #
    # result = ratlib.autocorrect.correct(event.message)
    # if not result.fixed:
    #     return
    names = ", ".join(
        '"...{old}" is probably "...{new}"'
            .format(old=old, new=new) for old, new in event.result.corrections.items()
    )

    if event.channel:
        event.say("{names} (corrected for {nick})".format(names=names, nick=event.nick))
    else:
        event.say(names)
