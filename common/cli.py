"""Consistent Chinese command-line output, including redirected Windows streams."""
import sys


def configure_output():
    for stream in (sys.stdout, sys.stderr):
        if callable(getattr(stream, 'reconfigure', None)):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
