"""Entry point of media-manager-update-wanted.service.

The work lives in maintenance/update_wanted.py, imported the same way
stack/reset-error.py imports maintenance/reset_error.py, so both scripts
follow one pattern; the tests import that module directly.
"""
import sys

from maintenance.update_wanted import main

if __name__ == '__main__':
    sys.exit(main())
