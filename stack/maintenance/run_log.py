"""Logging of the maintenance scripts: the journal always, a durable file under systemd."""
import logging
import logging.handlers
import os
import sys

# One file never exceeds 5 MiB, and five old ones are kept: 30 MiB at most.
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5


def setup(file_name, environ=os.environ):
    """Log INFO and above to stdout and, when systemd says where, to a file.

    stdout goes to the journal, which dates each line itself. systemd sets
    LOGS_DIRECTORY from the unit's LogsDirectory= (joining several entries
    with ':'; the first is used), and `file_name` is written there, rotated,
    so the history outlives the journal's retention.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    root.addHandler(stdout)

    logs_directory = environ.get('LOGS_DIRECTORY')
    if logs_directory:
        path = os.path.join(logs_directory.split(':')[0], file_name)
        durable = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding='utf-8')
        durable.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        root.addHandler(durable)
