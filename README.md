# pdrive

Command-line access to a **Proton Drive public share link** (a `https://drive.proton.me/urls/...` link,
optionally password-protected, with viewer or editor rights). Lists, uploads, downloads, creates folders,
renames and deletes, with `cp`/`ls`/`rm`-style commands.

Proton has no Python SDK, so pdrive is two parts:

- **`pdrive` (Python, stdlib only)**: the CLI, settings and credential caching, path resolution,
  recursion, skip/replace rules, retries, progress. This is where the logic and the tests are.
- **`src/pdrive/bridge_js/bridge.ts` (Node)**: a small JSON-lines process that wraps Proton's official
  [`@protontech/drive-sdk`](https://www.npmjs.com/package/@protontech/drive-sdk) and does one uid-based
  operation per request. It handles the encryption and the SRP login.

## Install

Needs Python ≥ 3.10 and Node ≥ 22.

1. Install the bridge's Node dependencies (the Proton SDK and `tsx`):

   ```sh
   npm ci --prefix src/pdrive/bridge_js
   ```

2. Install the `pdrive` command. Pick **one** of these:

   ```sh
   uv tool install -e .                  # password saved to a 0600 file
   uv tool install -e '.[keyring]'       # password saved to the system keyring
   ```

   With pip, the same choice is `pip install -e .` or `pip install -e '.[keyring]'`.

`[keyring]` is an optional extra. It installs the [`keyring`](https://pypi.org/project/keyring/) package,
and `pdrive login` then saves the share password in your desktop's secret store (GNOME Keyring or
KWallet via the Secret Service). Keep the quotes: without them, zsh treats the `[...]` as a glob pattern.

Without the extra, or when no keyring service is running (for example over SSH or under systemd),
pdrive falls back to `~/.config/pdrive/credentials`, a file readable only by you (mode 0600). Either way
the password is never written to the main config file. To force one store, run
`pdrive config set password_store keyring` (or `file`). See [Settings and credentials](#settings-and-credentials).

## Quick start

```sh
pdrive login 'https://drive.proton.me/urls/XXXXXXXXXX#key'   # prompts for the share password
pdrive ls -lhF
pdrive du -sh Movies
pdrive put -r ~/Videos/Season\ 2/ "TV/Show/Season 2"        # trailing / = contents of the folder
pdrive get -r Books/Calibre ./books
pdrive mkdir -p TV/New/Folder
pdrive rename "TV/New" "TV New"
pdrive rm -rn Old/Stuff                                      # dry run; drop -n to delete
```

Remote paths are always relative to the share root; `Movies`, `/Movies` and `/Movies/` are the same.

## Commands

| Command | What it does |
|---|---|
| `login [URL] [--password-stdin] [--no-store]` | Verify the link and password, then save them |
| `logout [--all]` | Forget the saved password (`--all`: and the URL) |
| `config [list\|get\|set\|unset\|path] [KEY] [VALUE]` | Show or change saved settings |
| `ls [-l] [-h] [-R] [-d] [-F] [--json] [PATH...]` | List folders. `--json` prints one object per line with the full path |
| `du [-s] [-h] [-d N] [PATH...]` | Folder totals |
| `stat [--json] PATH...` | Details of a node |
| `mkdir [-p] PATH...` | Create folders |
| `rename PATH NEW_NAME` | Rename in place. The link API can't move between folders |
| `rm [-r] [-f] [-i] [-n] PATH...` | Delete **permanently**, with no trash |
| `put [-r] [-u\|-f] [-n] [-p] [-j N] [--limit-rate R] [--exclude GLOB] LOCAL... REMOTE` | Upload |
| `get [-r] [-u\|-f] [-n] [-j N] [--exclude GLOB] REMOTE... LOCAL` | Download |

Global flags: `-v` reports each file, `-q` silences progress and the summary. Both work before or after
the command. Other global flags: `--share-url`, `--config-dir`, `--version`.

### Copy semantics (`put`, `get`)

These follow `cp -r`, plus rsync's trailing-slash rule:

- A single file can be copied to a new name: `put a.mkv Movies/b.mkv`.
- With several sources, the destination must be an existing folder.
- `put -r dir DEST` creates `DEST/dir` when `DEST` exists, and `DEST` itself when it doesn't.
- `put -r dir/ DEST` copies the *contents* of `dir` into `DEST`. Use this form when re-running an
  upload, so the run is idempotent.

When the destination file already exists:

| | same size | different size |
|---|---|---|
| default | skip | report a conflict (exit 1) |
| `-u`, `--update` | skip | replace (`put` uploads a new **revision**) |
| `-f`, `--force` | replace | replace |

So re-running an interrupted `put`/`get` skips what is already done. Downloads are written to
`*.pdrive-partial` and renamed when complete, and they keep the remote modification time. Network and
rate-limit errors are retried after 5 s, 30 s and 120 s. An expired link session is re-authenticated
once, automatically.

### Exit status and output

`0` success · `1` something failed (each failure is reported on stderr) · `2` usage error ·
`130` interrupted. Listings go to stdout. Progress, `-v` lines and errors go to stderr, so
`pdrive ls --json -R / | jq ...` works. The progress meter only appears when stderr is a terminal.

## Settings and credentials

Settings live in `~/.config/pdrive/config` (or `$XDG_CONFIG_HOME/pdrive`, or `$PDRIVE_CONFIG_DIR`).

| key | env | default | |
|---|---|---|---|
| `share_url` | `PDRIVE_SHARE_URL` | – | set by `login` |
| `jobs` | `PDRIVE_JOBS` | 4 | parallel transfers (the SDK runs at most 5 per session) |
| `limit_rate` | `PDRIVE_LIMIT_RATE` | 0 | upload cap, e.g. `20M` (bytes/s) |
| `dns` | `PDRIVE_DNS` | system | `cloudflare` resolves via 1.1.1.1, for when the router's DNS flakes |
| `node` | `PDRIVE_NODE` | PATH, then fnm default | node binary |
| `password_store` | `PDRIVE_PASSWORD_STORE` | auto | `keyring` \| `file` \| `auto` |

Precedence is: command-line flag, then environment, then config file, then default.

**The password is never written to the config file.** `login` saves it to the system keyring when the
optional `keyring` package has a working backend. Otherwise it goes to `~/.config/pdrive/credentials`,
mode `0600`. `PDRIVE_PASSWORD` overrides any saved password, which is useful for scripts. `--no-store`
saves only the URL, and pdrive then prompts for the password whenever stdin is a terminal.

## Limitations (from Proton, not pdrive)

- **Rename and delete only work on items created in the same link session.** Each pdrive command opens
  a new session, so items uploaded earlier, by anyone, can't be renamed or deleted through the link.
  The share owner can do it from their Proton account. pdrive reports this clearly instead of failing
  silently.
- No move between folders, because the link API has no move.
- Sizes are the size the uploader claimed. Folders have no size of their own, so `du` sums the files.

## Development

```sh
uv venv && uv pip install -e . pytest
.venv/bin/pytest                      # unit tests: fake backend + fake bridge; no network
PDRIVE_LIVE=1 .venv/bin/pytest -m live   # read-only checks against your logged-in share
```

The core (`drive.py`, `transfer.py`, `config.py`, `cli.py`) only talks to the `Backend` protocol in
`remote.py`. The tests use an in-memory `FakeBackend` (`tests/conftest.py`). `BridgeBackend`'s JSON-lines
handling is tested against `tests/fake_bridge.py`. Set `PDRIVE_BRIDGE_DEBUG=1` to see SDK logs.
