from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import typer
from rich.prompt import Confirm, Prompt

from cuswap import __version__, service, store
from cuswap.render import console, render_rows, render_status
from cuswap.store import StoreError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Manage and switch Cursor IDE accounts locally.",
)


def _fail(message: str) -> None:
    console.print(f"[red]{message}[/]")
    raise typer.Exit(code=1)


def _emit(data: object, *, as_json: bool) -> None:
    if as_json:
        console.print_json(data=data)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show version and exit."),
) -> None:
    if version:
        console.print(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        list_accounts()


@app.command("list")
def list_accounts(
    cached: bool = typer.Option(False, "--cached", help="Use last cached usage, skip API."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show saved accounts, live usage, and status."""
    try:
        rows = service.collect_rows(refresh=not cached)
    except Exception as exc:  # noqa: BLE001
        _fail(str(exc))
    if as_json:
        _emit(
            {
                "schemaVersion": 1,
                "accounts": [
                    {
                        "slot": row.account.slot,
                        "email": row.account.email,
                        "name": row.account.name,
                        "active": row.active,
                        "live": row.live,
                        "disabled": row.account.disabled,
                        "status": row.status,
                        "membershipType": (row.usage.membership_type if row.usage else None)
                        or row.account.membership_type,
                        "usage": row.usage.to_dict() if row.usage else None,
                    }
                    for row in rows
                ],
            },
            as_json=True,
        )
        return
    render_rows(rows)


@app.command("status")
def status(
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show the account currently signed into Cursor."""
    rows = service.collect_rows(refresh=True)
    current = next((row for row in rows if row.live or row.active), None)
    if not current:
        _fail("No active account. Sign in to Cursor and run `cuswap add`.")
    if as_json:
        _emit(
            {
                "schemaVersion": 1,
                "email": current.account.email,
                "slot": current.account.slot,
                "status": current.status,
                "usage": current.usage.to_dict() if current.usage else None,
            },
            as_json=True,
        )
        return
    render_status(current)


@app.command("add")
def add(
    slot: Optional[int] = typer.Option(None, "--slot", min=1, help="Force a slot number"),
) -> None:
    """Capture the account currently signed into Cursor IDE."""
    try:
        account = service.add_current(slot=slot)
    except service.ServiceError as exc:
        _fail(str(exc))
    console.print(f"[green]Saved[/] slot {account.slot}: [bold]{account.email}[/]")


@app.command("switch")
def switch(
    target: Optional[str] = typer.Argument(None, help="Slot number or email. Omit to rotate."),
    strategy: str = typer.Option(
        "next",
        "--strategy",
        help="When TARGET is omitted: next | best | next-available",
    ),
    restart: Optional[bool] = typer.Option(
        None,
        "--restart/--no-restart",
        help="Quit and relaunch Cursor after swapping (default: yes).",
    ),
    force: bool = typer.Option(False, "--force", help="Rewrite session even if already active."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Switch Cursor to a saved account. Restarts the IDE by default."""
    if strategy not in {"next", "best", "next-available"}:
        _fail("strategy must be next, best, or next-available")
    try:
        previous, dest = service.switch_to(
            target,
            strategy=strategy,
            restart=restart,
            force=force,
        )
    except (service.ServiceError, StoreError) as exc:
        _fail(str(exc))
    payload = {
        "schemaVersion": 1,
        "switched": True,
        "from": previous.email if previous else None,
        "to": dest.email,
        "slot": dest.slot,
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    src = previous.email if previous else "(none)"
    console.print(f"[green]Switching[/] {src} → [bold]{dest.email}[/] (slot {dest.slot})")
    if restart is not False:
        console.print("[dim]Cursor will close, then reopen on the new account. No extra CMD window should appear.[/]")


@app.command("apply-switch", hidden=True)
def apply_switch_cmd() -> None:
    """Internal: finish a switch after Cursor exits."""
    from cuswap.apply_switch import run

    raise typer.Exit(code=run())


@app.command("remove")
def remove(target: str = typer.Argument(..., help="Slot number or email")) -> None:
    """Remove a saved account (does not log out of Cursor)."""
    try:
        account = service.remove_account(target)
    except StoreError as exc:
        _fail(str(exc))
    console.print(f"Removed slot {account.slot}: {account.email}")


@app.command("disable")
def disable(target: str) -> None:
    """Hold an account out of rotation."""
    try:
        account = service.set_disabled(target, True)
    except StoreError as exc:
        _fail(str(exc))
    console.print(f"Disabled {account.email}")


@app.command("enable")
def enable(target: str) -> None:
    """Return a disabled account to rotation."""
    try:
        account = service.set_disabled(target, False)
    except StoreError as exc:
        _fail(str(exc))
    console.print(f"Enabled {account.email}")


@app.command("login")
def login(
    slot: Optional[int] = typer.Option(None, "--slot", min=1),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="After login, switch the IDE to this account (restarts Cursor).",
    ),
    timeout: int = typer.Option(240, "--timeout", min=30, help="Seconds to wait for browser login"),
) -> None:
    """Add/refresh an account in a private browser window — do not logout cursor.com."""
    console.print("[bold]Do not logout on your normal browser.[/]")
    console.print("Sign in as the other account in the private/incognito window only.")
    console.print("Cursor IDE stays on the current account until you [bold]cuswap switch[/].")

    def _started(url: str, opened: str | None) -> None:
        if opened:
            console.print(f"Opened private [bold]{opened}[/] window.")
        else:
            console.print("[yellow]Could not open a private window automatically.[/]")
        console.print(f"If needed, paste this URL into an InPrivate/Incognito window:\n{url}")
        console.print("[dim]Waiting for you to finish login…[/]")

    try:
        account, _url, opened = service.add_browser_account(
            slot=slot,
            apply=apply,
            timeout=float(timeout),
            on_start=_started,
        )
    except (service.ServiceError, Exception) as exc:  # noqa: BLE001
        _fail(str(exc))
    where = f"private {opened}" if opened else "browser login"
    console.print(f"[green]Saved[/] slot {account.slot}: [bold]{account.email}[/] via {where}")
    if apply:
        console.print("Cursor was switched to this account.")
    else:
        console.print(f"IDE unchanged. Switch later with [bold]cuswap switch {account.slot}[/]")


@app.command("watch")
def watch(
    interval: int = typer.Option(30, "--interval", min=10, help="Refresh seconds"),
) -> None:
    """Refresh the usage dashboard until Ctrl+C."""
    console.print(f"[dim]Refreshing every {interval}s. Ctrl+C to stop.[/]")
    try:
        while True:
            console.clear()
            rows = service.collect_rows(refresh=True)
            render_rows(rows, title="Cursor accounts (live)")
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/]")


@app.command("auto")
def auto(
    threshold: Optional[int] = typer.Option(None, "--threshold", min=1, max=100),
    interval: Optional[int] = typer.Option(None, "--interval", min=15),
    once: bool = typer.Option(False, "--once"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Switch to the account with most remaining quota when usage hits the threshold."""
    settings = store.load_settings()
    limit = threshold if threshold is not None else int(settings["autoswitch"]["threshold"])
    wait = interval if interval is not None else int(settings["autoswitch"]["interval"])
    console.print(f"[dim]Auto-switch at {limit}% · every {wait}s · strategy best[/]")
    if not once:
        console.print("[yellow]Switching will quit and restart Cursor.[/]")

    def tick() -> bool:
        rows = service.collect_rows(refresh=True)
        current = next((row for row in rows if row.live or row.active), None)
        if not current or not current.usage or current.usage.percent is None:
            console.print("[dim]No usable active account usage yet.[/]")
            return False
        render_rows(rows)
        if current.usage.percent < limit and not current.usage.exhausted:
            console.print(f"Active {current.account.email} at {current.usage.percent:.0f}% — staying")
            return False
        console.print(f"[yellow]{current.account.email} reached {current.usage.percent:.0f}%[/]")
        if dry_run:
            console.print("[dim]dry-run: would switch --strategy best[/]")
            return True
        try:
            _, dest = service.switch_to(None, strategy="best")
        except service.ServiceError as exc:
            console.print(f"[red]{exc}[/]")
            return False
        console.print(f"[green]Switched to {dest.email}[/]")
        return True

    try:
        while True:
            tick()
            if once:
                return
            time.sleep(wait)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/]")


@app.command("tui")
def tui() -> None:
    """Simple interactive menu."""
    while True:
        console.print()
        try:
            rows = service.collect_rows(refresh=True)
            render_rows(rows)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]{exc}[/]")
        console.print("[bold]1[/] switch  [bold]2[/] add  [bold]3[/] login  [bold]q[/] quit")
        choice = Prompt.ask("Choose", default="q").strip().lower()
        if choice in {"q", "quit", "exit"}:
            return
        if choice == "1":
            target = Prompt.ask("Slot / email (empty = next)", default="")
            try:
                _, dest = service.switch_to(target or None)
                console.print(f"[green]Switched to {dest.email}[/]")
            except (service.ServiceError, StoreError) as exc:
                console.print(f"[red]{exc}[/]")
        elif choice == "2":
            try:
                account = service.add_current()
                console.print(f"[green]Saved {account.email}[/]")
            except service.ServiceError as exc:
                console.print(f"[red]{exc}[/]")
        elif choice == "3":
            if Confirm.ask("Sign out Cursor to add another account?", default=False):
                service.prepare_login()
                console.print("Log in, then choose 2 / cuswap add.")
        else:
            console.print("[dim]Unknown choice[/]")


@app.command("config")
def config_cmd(
    action: Optional[str] = typer.Argument(None, help="get | set | path"),
    key: Optional[str] = typer.Argument(None),
    value: Optional[str] = typer.Argument(None),
) -> None:
    """Show or edit settings."""
    settings = store.load_settings()
    if action in {None, "show", "list"}:
        console.print_json(data=settings)
        return
    if action == "path":
        console.print(str(store.settings_path()))
        return
    if action == "get":
        if not key:
            _fail("config get KEY")
        console.print(json.dumps(_nested_get(settings, key), indent=2))
        return
    if action == "set":
        if not key or value is None:
            _fail("config set KEY VALUE")
        _nested_set(settings, key, _parse_value(value))
        store.save_settings(settings)
        console.print(f"Set {key} = {value}")
        return
    _fail("Unknown config action")


@app.command("doctor")
def doctor(
    repair: bool = typer.Option(False, "--repair", help="Fix a corrupted state.vscdb (Cursor must be closed)."),
) -> None:
    """Check Cursor paths and the current session."""
    if repair:
        try:
            note = service.repair_storage()
        except service.ServiceError as exc:
            _fail(str(exc))
        console.print(f"[green]Repair[/]: {note}")
    info = service.doctor()
    for key, value in info.items():
        console.print(f"[bold]{key}[/]: {value}")


@app.command("export")
def export_cmd(path: Path = typer.Argument(..., help="Output JSON path")) -> None:
    """Export saved sessions so another machine can import them without logging in."""
    index = store.load_index()
    if not index.accounts:
        _fail("Nothing to export.")
    try:
        count = store.export_accounts(path, index)
    except StoreError as exc:
        _fail(str(exc))
    console.print(f"Exported {count} account(s) to {path}")
    console.print("[yellow]This file contains session tokens. Keep it private and do not commit it.[/]")


@app.command("import")
def import_cmd(
    path: Path = typer.Argument(..., help="Export JSON from this or another machine"),
    replace: bool = typer.Option(
        False,
        "--replace",
        help="Delete accounts already saved on this machine, then import the file.",
    ),
    yes: bool = typer.Option(False, "--yes", help="Do not prompt when using --replace."),
) -> None:
    """Import sessions from an export file. Does not switch the open Cursor window."""
    if replace and not yes and not Confirm.ask(
        "Replace all saved accounts on this machine with the file?",
        default=False,
    ):
        raise typer.Exit()
    try:
        added, updated = store.import_accounts(path, replace=replace)
    except StoreError as exc:
        _fail(str(exc))
    console.print(f"Imported {added} new account(s), updated {updated}.")
    console.print("Run `cuswap list`, then `cuswap switch <slot>` to use one. Cursor stays as-is until then.")


@app.command("purge")
def purge(
    yes: bool = typer.Option(False, "--yes", help="Do not prompt"),
) -> None:
    """Delete all cuswap data on this machine."""
    if not yes and not Confirm.ask("Delete all saved Cursor sessions from cuswap?", default=False):
        raise typer.Exit()
    store.purge_all()
    console.print("Purged local cuswap data.")


@app.command("help")
def help_cmd() -> None:
    """Show command overview."""
    console.print(
        "cuswap — Cursor account switcher\n\n"
        "  cuswap list              Dashboard: accounts, usage, status\n"
        "  cuswap status            Current Cursor account\n"
        "  cuswap add               Save the signed-in Cursor session\n"
        "  cuswap switch [target]   Switch account (restarts Cursor)\n"
        "  cuswap login             Add another account in a private browser window\n"
        "  cuswap watch             Live usage dashboard\n"
        "  cuswap auto              Switch when usage hits the threshold\n"
        "  cuswap doctor            Check local Cursor paths\n"
        "  cuswap export <file>     Save sessions for another machine (contains tokens)\n"
        "  cuswap import <file>     Restore those sessions without logging in again\n"
        "\nRun cuswap COMMAND --help for details."
    )


def _nested_get(data: dict, dotted: str):
    cur = data
    for part in dotted.split("."):
        cur = cur[part]
    return cur


def _nested_set(data: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _parse_value(raw: str):
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError:
            return raw
