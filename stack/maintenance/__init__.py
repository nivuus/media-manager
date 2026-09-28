"""Code shared by the media-manager maintenance scripts.

stack/ is deployed as is to /opt/nivuus/media-manager, and systemd runs each
script as `python3 /opt/nivuus/media-manager/<script>.py`. Python puts that
directory first on sys.path, which is what makes this package importable
from the scripts without installing anything.
"""
