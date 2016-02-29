#coding: utf8
"""
autocorrect.py - System name autocorrection.
Copyright 2016, Daniel "dewin" Grace

Licensed under the Eiffel Forum License 2.

This is currently a very rudimentary implementation, lacking any sort of configuration.
"""
from ratbot import *
import ratlib.autocorrect


@rule(".+")
def correct_system(event):
    result = ratlib.autocorrect.correct(event.message)
    if not result.fixed:
        return
    names = ", ".join(
        '"...{old}" is probably "...{new}"'
            .format(old=old, new=new) for old, new in result.corrections.items()
    )
    event.say("{names} (corrected for {nick})".format(names=names, nick=event.nick))
