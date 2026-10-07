#!/usr/bin/env python3
"""Wizard answers of the subtitle translation (Lingarr) -> .env values.

Languages are taken as BCP-47 codes and only their SHAPE is checked, never
membership in a list: Lingarr turns a code into the language name its prompt
uses through .NET's CultureInfo, so any language the model can write works,
not just the defaults. A malformed code is refused by name rather than
passed on to fail later inside Lingarr.
"""
import json
import re

PROFILE = "lingarr"

# A BCP-47 tag: a 2-3 letter primary subtag, then optional script/region/
# variant subtags. ASCII by definition of the standard, whatever the script
# of the language it names.
_TAG = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*")


def language_list(value, key):
    """'en, pt-BR' -> the minified JSON list Lingarr reads from its env.

    Lingarr only shows `name` in its UI; it resolves the language itself
    from `code`, so the code doubles as the name instead of a hardcoded
    table of language names.
    """
    codes = [code.strip() for code in value.split(",") if code.strip()]
    if not codes:
        raise ValueError(f"the answer {key!r} needs at least one language code")
    for code in codes:
        if not _TAG.fullmatch(code):
            raise ValueError(f"the answer {key!r} has an invalid BCP-47 code: {code!r}")
    langs = [{"name": code, "code": code} for code in dict.fromkeys(codes)]
    # Single-quoted: the value holds double quotes, and both docker compose
    # and python-dotenv read a single-quoted .env value literally.
    return "'" + json.dumps(langs, separators=(",", ":")) + "'"


def translation_env(enabled, endpoint, model, api_key, sources, targets):
    """The .env values of the translation, and whether its profile is on.

    Turned on without an endpoint or a model, Lingarr would start and fail
    every job: that is refused here, before anything is laid down. Turned
    off, the values are still rendered so that switching it on later is one
    COMPOSE_PROFILES edit.
    """
    if enabled and not (endpoint and model):
        raise ValueError("subtitle_translation needs subtitle_translation_endpoint"
                         " and subtitle_translation_model")
    return {
        "SUBTITLE_TRANSLATION_ENDPOINT": endpoint,
        "SUBTITLE_TRANSLATION_MODEL": model,
        "SUBTITLE_TRANSLATION_API_KEY": api_key,
        "SUBTITLE_SOURCE_LANGUAGES": language_list(sources, "subtitle_source_languages"),
        "SUBTITLE_TARGET_LANGUAGES": language_list(targets, "subtitle_target_languages"),
    }
