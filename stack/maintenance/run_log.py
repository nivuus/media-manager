"""The maintenance scripts' logging and shared entry-point sequence.

stdout is what the journal keeps. When the unit declares LogsDirectory=,
systemd creates the directory and passes it as LOGS_DIRECTORY; the scripts
then also write a file there that outlives the journal's retention, rotated
so that it cannot fill the disk. entry_point() is what each script's own
main() calls: it sets the log up first, so that even a crash in
load_environment() reaches the durable log, then loads the environment and
runs the script.
"""
import logging
import logging.handlers
import os
import sys

from maintenance.arr_api import load_environment

log = logging.getLogger(__name__)

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


def entry_point(name, run):
    """The shared body of every maintenance script's main().

    Sets up logging to `<name>.log` first, so that even a crash in
    load_environment() reaches the durable log; then loads the environment
    and calls run(os.environ), returning its exit status. An unexpected
    exception is unexpected, so nothing is known to be safe: it is logged
    here with its traceback, and this returns 1 so that systemd marks the
    unit failed. SystemExit and KeyboardInterrupt are left uncaught: they
    already carry their own exit behaviour, and catching them here would be
    exactly the silent catch-and-continue this codebase avoids.
    """
    setup(f'{name}.log')
    try:
        load_environment()
        return run(os.environ)
    except Exception:
        log.exception('%s crashed', name)
        return 1
