# cuswap

**EN** | [**VI**](README.md)

A CLI to manage multiple **Cursor IDE** accounts on your machine — like [claude-swap](https://github.com/realiti4/claude-swap) (`cswap`), but for Cursor.

- List accounts, plan, usage, and status
- Switch accounts quickly by swapping a saved session
- **No** website login/logout every time you switch
- Cursor chat, settings, and extensions stay as they are (only the signed-in identity changes)

> Use only with accounts **you own**. Tokens are stored on your machine (Windows: DPAPI). The tool does **not** reset the machine ID and does **not** bypass billing.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
cd cuswap
uv sync
uv run cuswap --help
```

Install onto PATH:

```bash
uv tool install -e .
cuswap list
```

## Workflow

### 1. Save the account currently signed into Cursor

```bash
cuswap add
```

### 2. Add a second account (without website logout)

**Do not** Sign Out on cursor.com — that revokes the previous account’s session.

```bash
cuswap login
```

A private/incognito window opens. Sign in to the new account **there**. Your normal browser and Cursor IDE keep the current account.

Then switch when needed:

```bash
cuswap switch 2
```

`cuswap switch` must quit Cursor and reopen it. If you run the command **inside Cursor’s terminal**, the tool starts a helper process so the session is written after the IDE exits — otherwise the command is killed mid-write and the account does not change.

Each account only needs the private-window login **once**. After that, `cuswap switch` does not touch the website.

### 3. Check usage / status

```bash
cuswap                # dashboard (like cswap list)
cuswap list
cuswap status
cuswap watch          # periodic refresh
```

### 4. Switch accounts

```bash
cuswap switch                 # rotate to the next account
cuswap switch 2               # by slot
cuswap switch you@email.com
cuswap switch --strategy best # account with the most remaining quota
```

By default the tool **quits and reopens Cursor** so the IDE picks up the new session. Skip restart (not recommended):

```bash
cuswap switch 2 --no-restart
```

## Commands

| Command | Meaning |
|---------|---------|
| `cuswap` / `cuswap list` | Account table + usage + status |
| `cuswap status` | Currently active account |
| `cuswap add` | Save the signed-in session |
| `cuswap switch [target]` | Switch account |
| `cuswap login` | Add an account via private window |
| `cuswap disable 2` / `enable 2` | Exclude from / restore to rotation |
| `cuswap remove 2` | Remove from cuswap (does not delete the Cursor account) |
| `cuswap watch` | Live dashboard |
| `cuswap auto` | Auto-switch when usage hits a threshold (restarts Cursor) |
| `cuswap tui` | Interactive menu |
| `cuswap doctor` | Check paths / session |
| `cuswap doctor --repair` | Repair a corrupted `state.vscdb` (Cursor must be quit) |
| `cuswap export backup.json` | Export sessions to move machines (contains tokens — keep private) |
| `cuswap import backup.json` | Import sessions without logging in again |
| `cuswap import backup.json --replace` | Clear local accounts, then import exactly what’s in the file |
| `cuswap purge` | Delete all cuswap data |

`list`, `status`, and `switch` support `--json` for scripting.

## How it works

`cuswap` does **not** copy the whole `state.vscdb` file (that makes Cursor report *file storage corrupted*). It opens the DB in place, UPDATEs only auth keys, then checkpoints the WAL — the same atomic write style (`os.replace` + retry) as [claude-swap](https://github.com/realiti4/claude-swap).

If Cursor already reports storage corrupted, quit every Cursor window and run `cuswap doctor --repair`.

Cursor stores the session in SQLite:

- Windows: `%APPDATA%\Cursor\User\globalStorage\state.vscdb`
- macOS: `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
- Linux: `~/.config/Cursor/User/globalStorage/state.vscdb`

`cuswap` only reads/writes auth keys (`cursorAuth/*` and a few identity keys). Chat history and settings are left alone.

Usage comes from Cursor’s dashboard endpoint (`/api/usage-summary`) with the local session token — the same source the Usage page on cursor.com uses. This API is unofficial and may change.

`cuswap list` refreshes the token only when the access token is expired or the usage API returns 401. Cursor encrypts `accessToken` / `refreshToken` in `state.vscdb` (`cursor-safe-storage`); the tool decrypts locally before calling usage. `needs_reauth` means the session was actually revoked (Sign Out on the website/Cursor) — not merely because you just ran `switch`. In that case the tool keeps the last successful usage snapshot; sign that account back in, then `cuswap add`.

Tool data directory:

| OS | Directory |
|----|-----------|
| Windows | `%APPDATA%\cuswap\` |
| macOS | `~/Library/Application Support/cuswap/` |
| Linux | `~/.local/share/cuswap/` |

Override with the `CUSWAP_HOME` environment variable.

## Move accounts to another machine

On the machine that already has accounts:

```bash
cuswap export cuswap-export.json
```

Copy that file to the other machine via USB or a private folder. Do not commit or share it — it contains access and refresh tokens.

On the new machine, after installing cuswap:

```bash
cuswap import cuswap-export.json
cuswap list
cuswap switch 2
```

`import` only writes sessions into cuswap. An open Cursor window does not change until `cuswap switch`. Existing emails on the destination are updated; new emails are added. If tokens are still valid, you do not need to log in each account again.

## Notes

- Switching **requires quitting Cursor** — the IDE keeps the session in RAM and may overwrite the DB while still open.
- Expired token: sign that account back into Cursor, then `cuswap add` to refresh.
- `cuswap export` outputs session tokens. Do not commit or share that file.

## Dev

```bash
uv sync --extra dev
uv run pytest
uv run cuswap doctor
```
