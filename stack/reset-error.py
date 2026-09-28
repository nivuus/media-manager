"""Entry point of media-manager-reset-error.service.

The work lives in maintenance/reset_error.py: a file name with a hyphen
cannot be imported, and the tests import that module.
"""
import sys

from maintenance.reset_error import main

if __name__ == '__main__':
    sys.exit(main())
