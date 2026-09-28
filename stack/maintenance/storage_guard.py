"""Whether the storage reset-error acts on is evidently there.

storage_problems() is checked once per run, before any action: the
Downloads directory exists, and no root folder of any instance reports
itself inaccessible. Radarr/Sonarr check that from inside their containers,
which is the view the queue rules act on, and an instance whose rootfolder
endpoint cannot be read counts against the storage. When a check fails,
the run changes nothing at all.

These checks cannot see a media disk missing at boot. Docker then recreates
every missing bind source as an empty directory on the root filesystem: the
library directories bazarr and plex bind one by one (MOVIES_DIR, TV_DIR) and
the Downloads directory rdtclient and prowlarr bind. The root folders exist,
so they report accessible, and the guard passes. Files-gone, the rule that
removes a row because its download is absent from Downloads, would then
read every download as gone, so library_doubt() withholds it while no
library directory holds anything. The other rules still act on the apps'
own messages, which a missing disk makes unreliable too.
"""
import os

from maintenance.arr_api import ApiError, get_json_list

# The library directories, as the unit's environment names them.
LIBRARY_VARIABLES = ('MOVIES_DIR', 'TV_DIR')


def storage_problems(instances, downloads_dir):
    """What says the storage is unavailable; an empty list when nothing does.

    An instance without root folders adds no evidence either way; one whose
    rootfolder endpoint cannot be read counts against the storage.
    """
    problems = []
    if not os.path.exists(downloads_dir):
        problems.append(f'downloads directory {downloads_dir} missing')
    elif not os.path.isdir(downloads_dir):
        problems.append(f'downloads directory {downloads_dir} is not a directory')
    for instance in instances:
        try:
            folders = get_json_list(instance, 'rootfolder')
        except ApiError as error:
            problems.append(f'rootfolder endpoint unreadable on {instance} ({error})')
            continue
        problems.extend(f"root folder {folder.get('path')} inaccessible on {instance}"
                        for folder in folders if folder.get('accessible') is False)
    return problems


def library_doubt(environ):
    """Why a download's absence from Downloads proves nothing; None when it does.

    It proves something only while the media disk is evidently mounted: at
    least one library directory holds an entry. A directory Docker recreated
    is empty, and so is the library of a fresh install, which then waits for
    its first import to have files-gone. Raises OSError when a library
    directory exists but cannot be listed: whether it holds anything is then
    unknown.
    """
    reasons = []
    for variable in LIBRARY_VARIABLES:
        path = environ.get(variable)
        if not path:
            reasons.append(f'{variable} is not set')
            continue
        try:
            if os.listdir(path):
                return None
            reasons.append(f'{variable} {path} is empty')
        except FileNotFoundError:
            reasons.append(f'{variable} {path} does not exist')
        except NotADirectoryError:
            reasons.append(f'{variable} {path} is not a directory')
    return '; '.join(reasons)
