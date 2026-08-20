#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests",
# ]
# ///
"""
Recreate Wiki.js pages on a new instance from an "export to disk" pages.json dump.

Targets the Wiki.js 2.x GraphQL API (the `pages.create` mutation). See README.md.

Quick start (uv handles the venv + deps automatically, nothing is installed
system-wide):

    uv run wikijs_migrate.py --export pages.sample.json.gz

Settings resolve in this order, first match wins:

    CLI flag  >  environment variable  >  config.toml  >  built-in default

Runs are DRY by default; pass --live to actually create pages.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import tomllib
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "url": None,
    "token": None,
    "export": "./pages.json.gz",
    "delay": 0.3,
    "timeout": 30,
    "skip_existing": True,
    "dry_run": True,
}

# Config keys that may be supplied via environment, as WIKIJS_<KEY>.
ENV_PREFIX = "WIKIJS_"

GZIP_MAGIC = b"\x1f\x8b"

# Wiki.js 2.x returns this when a page already lives at the requested path.
DUPLICATE_ERROR_CODES = {6002}

CREATE_MUTATION = """
mutation (
  $content: String!
  $description: String!
  $editor: String!
  $isPublished: Boolean!
  $isPrivate: Boolean!
  $locale: String!
  $path: String!
  $tags: [String]!
  $title: String!
) {
  pages {
    create(
      content: $content
      description: $description
      editor: $editor
      isPublished: $isPublished
      isPrivate: $isPrivate
      locale: $locale
      path: $path
      tags: $tags
      title: $title
    ) {
      responseResult {
        succeeded
        errorCode
        message
      }
      page {
        id
        path
      }
    }
  }
}
"""

LIST_QUERY = """
query {
  pages {
    list {
      id
      path
      locale
    }
  }
}
"""


# ---------------------------------------------------------------- config


def parse_bool(value, source):
    """Accept the usual truthy/falsy spellings from env vars and TOML."""
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise SystemExit(f"Invalid boolean for {source}: {value!r}")


def find_config(explicit):
    """Explicit path wins; otherwise look beside the CWD, then beside the script."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise SystemExit(f"Config file not found: {path}")
        return path
    for candidate in (Path.cwd() / "config.toml", SCRIPT_DIR / "config.toml"):
        if candidate.is_file():
            return candidate
    return None


def load_config(path):
    if path is None:
        return {}
    with open(path, "rb") as fh:
        try:
            return tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"Could not parse {path}: {exc}") from exc


def resolve(args, config):
    """Merge CLI flags, environment, config file and defaults into one dict."""
    settings = {}
    for key, fallback in DEFAULTS.items():
        cli = getattr(args, key, None)
        env = os.environ.get(f"{ENV_PREFIX}{key.upper()}")
        if cli is not None:
            value, source = cli, f"--{key.replace('_', '-')}"
        elif env is not None:
            value, source = env, f"{ENV_PREFIX}{key.upper()}"
        elif key in config:
            value, source = config[key], f"config: {key}"
        else:
            value, source = fallback, "default"

        if value is not None and isinstance(fallback, bool):
            value = parse_bool(value, source)
        elif value is not None and isinstance(fallback, (int, float)) and not isinstance(fallback, bool):
            try:
                value = type(fallback)(value)
            except (TypeError, ValueError) as exc:
                raise SystemExit(f"Invalid number for {source}: {value!r}") from exc

        settings[key] = value
    return settings


# ---------------------------------------------------------------- export


def load_export(path):
    """Read the export, transparently handling gzipped and plain JSON."""
    export = Path(path).expanduser()
    if not export.is_file():
        raise SystemExit(f"Export not found: {export}")

    with open(export, "rb") as fh:
        compressed = fh.read(2) == GZIP_MAGIC

    opener = gzip.open if compressed else open
    try:
        with opener(export, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, gzip.BadGzipFile) as exc:
        raise SystemExit(f"Could not read {export}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{export} is not valid JSON: {exc}") from exc

    # Wiki.js writes a bare list, but tolerate a wrapper object too.
    if isinstance(data, dict):
        for key in ("pages", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break

    if not isinstance(data, list):
        raise SystemExit(f"{export} does not contain a list of pages.")

    return data, ("gzip" if compressed else "plain JSON")


def normalize_tags(raw_tags):
    """Export tags may be plain strings or dicts with a 'tag' field — handle both."""
    out = []
    for tag in raw_tags or []:
        if isinstance(tag, str):
            out.append(tag)
        elif isinstance(tag, dict) and "tag" in tag:
            out.append(tag["tag"])
    return out


def to_variables(page):
    return {
        "content": page.get("content", ""),
        "description": page.get("description", ""),
        "editor": page.get("editorKey", "markdown"),
        "isPublished": bool(page.get("isPublished", 1)),
        "isPrivate": bool(page.get("isPrivate", 0)),
        "locale": page.get("localeCode", "en"),
        "path": page.get("path", ""),
        "tags": normalize_tags(page.get("tags")),
        "title": page.get("title", page.get("path", "Untitled")),
    }


# ---------------------------------------------------------------- api


def post(endpoint, headers, payload, timeout):
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    try:
        return resp.json(), None
    except ValueError:
        return None, f"non-JSON response, HTTP {resp.status_code}: {resp.text[:300]}"


def fetch_existing(endpoint, headers, timeout):
    """Return {(path, locale)} already on the target, or None if unavailable."""
    try:
        data, err = post(endpoint, headers, {"query": LIST_QUERY}, timeout)
    except requests.RequestException as exc:
        print(f"! Could not list existing pages ({exc}); relying on duplicate errors instead.")
        return None

    if err or not data or "errors" in (data or {}):
        detail = err or data.get("errors")
        print(f"! Could not list existing pages ({detail}); relying on duplicate errors instead.")
        return None

    try:
        listing = data["data"]["pages"]["list"]
    except (KeyError, TypeError):
        print("! Unexpected response listing pages; relying on duplicate errors instead.")
        return None

    return {(item.get("path"), item.get("locale")) for item in listing}


def is_duplicate(result):
    code = result.get("errorCode")
    message = (result.get("message") or "").lower()
    return code in DUPLICATE_ERROR_CODES or "already exists" in message


# ---------------------------------------------------------------- main


def build_parser():
    parser = argparse.ArgumentParser(
        description="Recreate Wiki.js 2.x pages on a new instance from an export dump.",
        epilog="Settings resolve as: CLI flag > WIKIJS_* env var > config.toml > default.",
    )
    parser.add_argument("--config", metavar="PATH", help="config file (default: ./config.toml)")
    parser.add_argument("--url", help="target wiki base URL, no trailing slash")
    parser.add_argument("--token", help="API token (prefer WIKIJS_TOKEN over this)")
    parser.add_argument("--export", metavar="PATH", help="pages.json or pages.json.gz to import")
    parser.add_argument("--delay", type=float, metavar="SECONDS", help="pause between page creations")
    parser.add_argument("--timeout", type=float, metavar="SECONDS", help="per-request HTTP timeout")

    run_mode = parser.add_mutually_exclusive_group()
    run_mode.add_argument("--live", dest="dry_run", action="store_false", default=None,
                          help="actually create pages (default is a dry run)")
    run_mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=None,
                          help="print the plan and change nothing (default)")

    skip = parser.add_mutually_exclusive_group()
    skip.add_argument("--skip-existing", dest="skip_existing", action="store_true", default=None,
                      help="skip paths already present on the target (default)")
    skip.add_argument("--no-skip-existing", dest="skip_existing", action="store_false", default=None,
                      help="attempt every page, letting the server reject duplicates")
    return parser


def main():
    args = build_parser().parse_args()
    config_path = find_config(args.config)
    settings = resolve(args, load_config(config_path))

    pages, kind = load_export(settings["export"])
    dry_run = settings["dry_run"]

    print(f"Config:  {config_path if config_path else 'none found (defaults + CLI/env)'}")
    print(f"Export:  {settings['export']} ({kind}, {len(pages)} pages)")
    print(f"Target:  {settings['url'] or '(not set)'}")
    print(f"Mode:    {'DRY RUN — nothing will be written' if dry_run else 'LIVE'}\n")

    if not settings["url"] or not settings["token"]:
        if not dry_run:
            missing = [n for n, v in (("url", settings["url"]), ("token", settings["token"])) if not v]
            raise SystemExit(
                f"Missing required setting(s) for a live run: {', '.join(missing)}.\n"
                f"Set {ENV_PREFIX}TOKEN in your environment, or pass --url/--token."
            )
        endpoint = headers = None
    else:
        endpoint = f"{settings['url'].rstrip('/')}/graphql"
        headers = {
            "Authorization": f"Bearer {settings['token']}",
            "Content-Type": "application/json",
        }

    existing = None
    if settings["skip_existing"]:
        if endpoint:
            existing = fetch_existing(endpoint, headers, settings["timeout"])
            if existing is not None:
                print(f"Target already holds {len(existing)} pages; matching paths will be skipped.\n")
        elif dry_run:
            print("Note: no URL/token, so existing pages on the target can't be checked.\n")

    created, skipped, failed = 0, 0, []

    for page in pages:
        variables = to_variables(page)
        path, locale = variables["path"], variables["locale"]
        label = f'{path}  ("{variables["title"]}", editor={variables["editor"]})'

        if existing is not None and (path, locale) in existing:
            skipped += 1
            print(f"~ {label}\n   SKIPPED (already on target)")
            continue

        print(f"-> {label}")

        if dry_run:
            continue

        try:
            data, err = post(endpoint, headers,
                             {"query": CREATE_MUTATION, "variables": variables},
                             settings["timeout"])
        except requests.RequestException as exc:
            print(f"   FAILED (request error): {exc}")
            failed.append(path)
            continue

        if err:
            print(f"   FAILED ({err})")
            failed.append(path)
            continue

        if "errors" in data:
            print(f"   FAILED (GraphQL error): {data['errors']}")
            failed.append(path)
            continue

        try:
            create = data["data"]["pages"]["create"]
            result = create["responseResult"]
        except (KeyError, TypeError):
            print(f"   FAILED (unexpected response): {json.dumps(data)[:300]}")
            failed.append(path)
            continue

        if result["succeeded"]:
            created += 1
            print(f"   OK (page id {create['page']['id']})")
        elif settings["skip_existing"] and is_duplicate(result):
            skipped += 1
            print("   SKIPPED (already on target)")
        else:
            print(f"   FAILED ({result['errorCode']}): {result['message']}")
            failed.append(path)

        if settings["delay"]:
            time.sleep(settings["delay"])

    print("\n---")
    if dry_run:
        planned = len(pages) - skipped
        print(f"DRY RUN complete. {planned} page(s) would be created, {skipped} skipped.")
        print("Re-run with --live to apply.")
        return 0

    print(f"Done. {created}/{len(pages)} created, {skipped} skipped, {len(failed)} failed.")
    if failed:
        print("Failed paths:")
        for path in failed:
            print(f"  - {path}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
