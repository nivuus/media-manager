"""Logging of the maintenance scripts."""
import logging
import sys


def setup():
    """Log INFO and above to stdout, which systemd stores in the journal."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
