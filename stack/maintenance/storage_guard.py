"""Whether the storage reset-error acts on is evidently there.

Checked once per run, before any action. With the media disk unmounted,
Docker recreates the bind sources as empty directories and Radarr/Sonarr see
every download as holding no files: their own messages ("No files found are
eligible for import") then say nothing true about the downloads, and every
rule would act on that broken view.

The evidence: the Downloads directory exists, and no root folder of any
instance reports itself inaccessible. Radarr/Sonarr check that from inside
their containers, which is exactly the view the queue rules act on. An empty
library proves nothing (a fresh install has one).
"""
import os

from maintenance.arr_api import ApiError, get_json_list


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
