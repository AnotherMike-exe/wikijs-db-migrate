# wikijs-db-migrate

Move a [Wiki.js](https://js.wiki/) 2.x instance between database backends — MariaDB, MySQL,
SQLite, PostgreSQL — by replaying its pages through the GraphQL API.

Wiki.js gives you no way to switch backends, and a `mysqldump` will not restore into Postgres.
So instead of converting the database, this exports your pages, then recreates them on a clean
instance running the new backend. It only speaks the public API, so it neither knows nor cares
which database sits behind either end.

- **Pages only.** Assets, page history, users and permissions do not come across ([details](#what-carries-over)).
- **Wiki.js 2.x only.** 3.0 replaces this API — get onto Postgres under 2.x first, then upgrade.
- **Dry by default.** Nothing is written until you pass `--live`.

---

## Usage

You need [uv](https://docs.astral.sh/uv/) and nothing else — it fetches Python and `requests`
into a throwaway environment, installs nothing system-wide, and leaves nothing to clean up.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # or: brew install uv
```

Try it on the bundled sample first. No wiki, no token, no setup:

```bash
uv run wikijs_migrate.py --export pages.sample.json.gz
```

Then the real thing, in four steps:

**1. Export the old wiki.** *Administration → Utilities → Export*, tick **Pages**, export to
disk. You get a `pages.json.gz`.

**2. Stand up the new instance** on the backend you're moving to. A fresh Wiki.js 2.x install
against the new database, setup wizard completed. Import nothing yet.

**3. Create an API token on the new wiki.** *Administration → API* → enable the API if it's off
→ **New API Key**, in a group that can create pages. Copy it now, it is shown only once.

**4. Dry-run, then migrate.**

```bash
export WIKIJS_TOKEN="your-token-here"

# Prints exactly what would be created. Writes nothing.
uv run wikijs_migrate.py --url https://wiki.example.com --export ./pages.json.gz

# Plan looks right? Add --live.
uv run wikijs_migrate.py --url https://wiki.example.com --export ./pages.json.gz --live
```

Then copy your asset files across and rebuild your navigation — neither comes through the API.

That's the whole job. Everything below is detail you only need if something is unusual.

---

## Options

Set anything three ways, resolved **CLI flag > environment variable > `config.toml` > default**.

| Flag | Env var | Default | Purpose |
| --- | --- | --- | --- |
| `--url URL` | `WIKIJS_URL` | — | Target wiki base URL, no trailing slash. `/graphql` is appended for you |
| `--token TOKEN` | `WIKIJS_TOKEN` | — | API token. Prefer the env var |
| `--export PATH` | `WIKIJS_EXPORT` | `./pages.json.gz` | Export to read. Gzip or plain JSON, auto-detected |
| `--live` / `--dry-run` | `WIKIJS_DRY_RUN` | dry | Whether to actually create pages |
| `--skip-existing` / `--no-skip-existing` | `WIKIJS_SKIP_EXISTING` | skip | Skip paths already on the target |
| `--delay SECONDS` | `WIKIJS_DELAY` | `0.3` | Pause between creations |
| `--timeout SECONDS` | `WIKIJS_TIMEOUT` | `30` | Per-request HTTP timeout |
| `--config PATH` | — | `./config.toml` | Config file location |

For a config file, `cp config.example.toml config.toml` and edit. The script looks in the
current directory, then next to itself. `config.toml` is gitignored.

```toml
url           = "https://wiki.example.com"
export        = "./pages.json.gz"
delay         = 0.3
skip_existing = true
dry_run       = true
```

---

## Keep your token and your export out of git

A Wiki.js API token is a full-access credential — anyone holding it can read and rewrite your
entire wiki. Keep it in `WIKIJS_TOKEN`, not in a file. That is why `config.example.toml` ships
without a `token` key even though the script would accept one. If a token does get committed
or pasted somewhere public, **revoke it** in *Administration → API* rather than just deleting
the text.

Your export is equally sensitive: it contains every page's full content in plaintext, including
whatever you happened to document in there. The bundled `.gitignore` keeps both out of version
control.

---

## What carries over

**Carried:** path, title, description, content, locale, tags, editor type, published and
private flags.

**Not carried:** assets, page history, users, groups, permissions, comments, navigation. Every
page arrives owned by whoever the token belongs to, with a fresh timestamp. A page embedding
`/img/diagram.png` keeps the link, but the file needs copying separately.

If page history matters to you, this is the wrong tool — you want a same-backend dump/restore
and no database change at all.

---

## Re-running after a partial migration

Just run it again. `--skip-existing` is on by default: the script asks the target which pages
it already has and skips those paths, and also treats a "page already exists" error as a skip
rather than a failure — so a re-run works even if the listing query is unavailable to your
token.

Exit code is `0` when everything succeeded or was skipped, `1` if any page failed.

---

## Export formats

Both work, and the script sniffs gzip magic bytes rather than trusting the extension, so a
mis-named file is fine:

```bash
uv run wikijs_migrate.py --export ./pages.json.gz
uv run wikijs_migrate.py --export ./pages.json
```

It expects a JSON array of page objects (a `{"pages": [...]}` wrapper also works). Only these
fields are read; anything else in the export is ignored:

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

`pages.sample.json` and `pages.sample.json.gz` are a three-page example of that shape.

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `Missing required setting(s) for a live run` | No `--url` or no token. Dry runs need neither; live runs need both |
| Every page fails with an auth error | API disabled on the target, or the token's group lacks `write:pages` |
| Everything is skipped | The pages are already there. Expected on a re-run |
| Pages arrive unrendered | That editor isn't enabled on the destination |
| Links between pages 404 | Wiki.js paths are absolute. If the old wiki lived under a path prefix, find-and-replace the content before importing |
| Images broken | Expected — assets aren't in the page export. Copy them across separately |

---

Licensed under the [MIT Licence](LICENSE), with no warranty. Dry-run it, and back up the target database first.
