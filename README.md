# wikijs-db-migrate

Move a [Wiki.js](https://js.wiki/) 2.x instance between database backends — MariaDB, MySQL,
SQLite, PostgreSQL — by replaying its pages through the GraphQL API.

One small Python script. No install step, no schema conversion, no dump juggling.

## Why this exists

Wiki.js 2.x runs on MariaDB/MySQL, PostgreSQL, SQLite or MSSQL, but it gives you no way to
move between them. A `mysqldump` will not restore into Postgres — different types, different
sequences, different quoting — and hand-converting the dump is miserable and fragile.

The reliable route is to sidestep the database entirely: export the pages from the old
instance, stand up a clean instance on the new backend, and recreate the pages through the
API. That is all this script does. Because it only speaks the public API, **it neither knows
nor cares which database sits behind either end** — MariaDB → PostgreSQL, SQLite → MariaDB, or
just an old host to a new one, all the same operation.

The common reason to do this right now is PostgreSQL. If you are eyeing the 3.0 upgrade, the
sensible first step is getting your 2.x content onto Postgres while everything still works.

> **This is not a schema converter.** It does not read, write or translate your database. It
> reads a JSON export and calls `pages.create` on the target, so the target must be a working,
> already-installed Wiki.js **2.x** instance. Wiki.js 3.0 replaces this API — migrate to a 2.x
> Postgres instance first, then follow the official 3.0 upgrade path from there.

---

## What carries over, and what doesn't

**Carried:** path, title, description, content, locale, tags, editor type, published and
private flags.

**Not carried:** page history, uploaded assets, users, groups, permissions, comments, and
navigation. Every page arrives owned by whoever the API token belongs to, with a fresh
timestamp. Assets need copying separately — a page embedding `/img/diagram.png` keeps the
link, but the file has to get there some other way (copy the old instance's asset directory,
or re-upload).

If page history matters to you, this is the wrong tool — you want a same-backend dump/restore
and no database change at all.

---

## Requirements

- [uv](https://docs.astral.sh/uv/) — that's the whole list. It fetches Python and `requests`
  into a cached, throwaway environment. Nothing lands in your system or user site-packages,
  and there is nothing to clean up afterwards.
- Python 3.11+, which uv pulls in automatically if you don't have it.
- An **API token** on the target wiki with page-creation rights.

Installing uv, if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # macOS / Linux
brew install uv                                    # or via Homebrew
```

---

## The migration, end to end

**1. Export the old wiki.** *Administration → Utilities → Export*, tick **Pages**, export to
disk. You get `pages.json.gz` (older builds write a plain `pages.json` — both work here).

**2. Stand up the new instance** on the backend you're moving to. Install Wiki.js 2.x fresh
against the new database and complete the setup wizard. Don't try to import anything yet.

**3. Make an API token on the new wiki.** *Administration → API* → enable the API if it's off
→ **New API Key**. Give it a group that can create pages; the built-in Administrators group
works. Copy the token immediately, it is shown only once.

**4. Dry-run, then migrate:**

```bash
export WIKIJS_TOKEN="your-token-here"

# Prints exactly what would be created, writes nothing
uv run wikijs_migrate.py --url https://wiki.example.com --export ./pages.json.gz

# Happy with the plan? Add --live
uv run wikijs_migrate.py --url https://wiki.example.com --export ./pages.json.gz --live
```

**5. Copy your assets across** if the old wiki had uploads, and re-check your navigation —
neither comes through the page API.

---

## Try it first

The bundled sample needs no wiki and no token:

```bash
uv run wikijs_migrate.py --export pages.sample.json.gz
```

That prints the plan for three fake pages and exits. Or make the script executable and drop
the `uv run` prefix — the shebang handles it:

```bash
chmod +x wikijs_migrate.py
./wikijs_migrate.py --live
```

**Runs are dry by default.** You have to ask for `--live` to write anything.

---

## Configuration

Three ways to set anything, resolved **CLI flag > environment variable > `config.toml` > default**.

### Config file

```bash
cp config.example.toml config.toml
$EDITOR config.toml
```

```toml
url           = "https://wiki.example.com"
export        = "./pages.json.gz"
delay         = 0.3
timeout       = 30
skip_existing = true
dry_run       = true
```

The script looks for `config.toml` in the current directory, then next to the script. Point it
elsewhere with `--config /path/to/other.toml`. `config.toml` is gitignored.

### Environment variables

Any setting, prefixed and upper-cased:

```bash
export WIKIJS_URL="https://wiki.example.com"
export WIKIJS_TOKEN="your-token-here"
export WIKIJS_EXPORT="./pages.json.gz"
export WIKIJS_DRY_RUN=false
```

### CLI flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--url URL` | — | Target wiki base URL, no trailing slash. `/graphql` is appended for you |
| `--token TOKEN` | — | API token. Prefer `WIKIJS_TOKEN` — see below |
| `--export PATH` | `./pages.json.gz` | Export to read. Gzip or plain JSON, auto-detected |
| `--config PATH` | `./config.toml` | Config file location |
| `--live` | off | Actually create pages |
| `--dry-run` | **on** | Print the plan, change nothing |
| `--skip-existing` | **on** | Skip paths already on the target |
| `--no-skip-existing` | off | Try every page, let the server reject duplicates |
| `--delay SECONDS` | `0.3` | Pause between creations |
| `--timeout SECONDS` | `30` | Per-request HTTP timeout |

### About the token

> **Keep your token out of `config.toml`, and out of any repo.** A Wiki.js API token is a
> full-access credential — anyone holding it can read and rewrite your entire wiki. Use
> `WIKIJS_TOKEN` in your shell environment. That is why `config.example.toml` ships without a
> `token` key even though the script would accept one.
>
> If a token does end up committed or pasted somewhere public, **revoke it** in
> *Administration → API* rather than just deleting the text. Rotating takes ten seconds.

Note also that a Wiki.js export contains **every page's full content in plaintext**, including
anything you documented in it. Treat `pages.json.gz` as sensitive and keep it out of version
control — the bundled `.gitignore` already does that.

---

## Re-running after a partial migration

`--skip-existing` is on by default, so re-running after a failure is safe. Before creating
anything, the script asks the target which pages it already has and skips those paths. It also
treats a "page already exists" error at creation time as a skip rather than a failure — so
even if the listing query is unavailable (a token without read permission, say), a re-run
won't produce a wall of red.

That makes the sane recovery from a half-finished migration simply: run it again.

The exit code is `0` when everything succeeded or was skipped, and `1` if any page failed, so
this drops into a script or CI job cleanly.

---

## Export formats

Point `--export` at either form; the script sniffs the gzip magic bytes rather than trusting
the file extension, so a mis-named file still works:

```bash
uv run wikijs_migrate.py --export ./pages.json.gz     # compressed
uv run wikijs_migrate.py --export ./pages.json        # plain
```

The expected shape is a JSON array of page objects (a `{"pages": [...]}` wrapper is also
accepted). Only these fields are read — anything else in the export is ignored:

| Field | Falls back to |
| --- | --- |
| `path` | `""` |
| `title` | `path`, then `"Untitled"` |
| `content` | `""` |
| `description` | `""` |
| `editorKey` | `"markdown"` |
| `localeCode` | `"en"` |
| `isPublished` | `true` |
| `isPrivate` | `false` |
| `tags` | `[]` — accepts `["a", "b"]` or `[{"tag": "a"}]` |

`pages.sample.json` and `pages.sample.json.gz` are a minimal three-page example of that shape,
including both tag spellings and one unpublished draft.

---

## Troubleshooting

**`Missing required setting(s) for a live run`** — no `--url` or no token. Dry runs work
without either; live runs need both.

**Every page fails with an auth error** — the API is disabled on the target
(*Administration → API*), or the token's group lacks `write:pages`.

**Everything is skipped** — the pages are already there. Expected on a re-run; pass
`--no-skip-existing` if you want to watch the server reject them individually.

**Pages arrive but look unrendered** — the target is missing that editor. A page exported from
the markdown editor needs the markdown editor enabled on the destination.

**Links between pages 404** — Wiki.js paths are absolute from the wiki root. If the old wiki
lived under a path prefix and the new one doesn't, the content needs a find-and-replace before
import.

**Everything worked but images are broken** — expected. Assets are not part of the page
export; copy them across separately.

---

## Licence

MIT. No warranty — dry-run it, and take a backup of the target database first.
