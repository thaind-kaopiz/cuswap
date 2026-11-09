from __future__ import annotations

import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table
from rich.text import Text

from cuswap.models import AccountRow, UsageSnapshot


def _configure_stdio() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_stdio()
console = Console(legacy_windows=False, highlight=False)


def _supports_unicode() -> bool:
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower()
    return encoding.startswith("utf")

STATUS_STYLE = {
    "active": "bold green",
    "ok": "green",
    "exhausted": "red",
    "token_expired": "red",
    "needs_reauth": "yellow",
    "disabled": "dim",
    "usage_error": "yellow",
    "missing_token": "red",
    "stale": "yellow",
}


def render_rows(rows: list[AccountRow], *, title: str = "Cursor accounts") -> None:
    table = Table(title=title, show_lines=False, expand=False)
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("Email", no_wrap=True)
    table.add_column("Plan", no_wrap=True)
    table.add_column("Usage", no_wrap=True)
    table.add_column("Reset", no_wrap=True)
    table.add_column("Status", no_wrap=True)

    if not rows:
        console.print("[dim]No accounts yet. Sign in to Cursor, then run[/] [bold]cuswap add[/].")
        return

    for row in rows:
        account = row.account
        slot = f"*{account.slot}" if row.active else str(account.slot)
        email = account.email
        if row.live and not row.active:
            email = f"{email} [dim](live)[/]"
        usage = _usage_cell(row.usage, stale=row.token_status not in {"ok", "usage_error"} and bool(row.usage and not row.usage.error))
        plan = (row.usage.membership_type if row.usage and row.usage.membership_type else None) or (
            account.membership_type or "-"
        )
        reset = _reset_cell(row.usage)
        status = Text(row.status, style=STATUS_STYLE.get(row.status, ""))
        table.add_row(
            slot,
            email,
            plan,
            usage,
            reset,
            status,
        )
    console.print(table)


def render_status(row: AccountRow) -> None:
    account = row.account
    console.print(f"[bold]Active:[/] {account.email}  (slot {account.slot})")
    if account.name:
        console.print(f"[bold]Name:[/]   {account.name}")
    if row.usage:
        console.print(f"[bold]Plan:[/]   {row.usage.membership_type or account.membership_type or '-'}")
        if row.usage.auto_percent is not None or row.usage.api_percent is not None:
            if row.usage.auto_percent is not None:
                console.print(f"[bold]Cursor models:[/] {_fmt_pct(row.usage.auto_percent)}")
            if row.usage.api_percent is not None:
                console.print(f"[bold]Other models:[/]  {_fmt_pct(row.usage.api_percent)}")
        else:
            console.print(f"[bold]Usage:[/]  {_usage_cell(row.usage)}")
        console.print(f"[bold]Reset:[/]  {_reset_cell(row.usage)}")
    console.print(f"[bold]Status:[/] {row.status}")


def _usage_cell(usage: UsageSnapshot | None, *, stale: bool = False) -> str:
    if usage is None:
        return "[dim]-[/]"
    if usage.error and not (usage.used is not None or usage.percent is not None):
        return f"[yellow]{usage.error}[/]"
    if usage.is_unlimited:
        body = "[green]unlimited[/]"
    elif usage.auto_percent is not None or usage.api_percent is not None:
        cursor_pct = usage.auto_percent
        other_pct = usage.api_percent
        parts = []
        if cursor_pct is not None:
            parts.append(f"C {_fmt_pct(cursor_pct)}")
        if other_pct is not None:
            parts.append(f"O {_fmt_pct(other_pct)}")
        body = " · ".join(parts) if parts else "[dim]-[/]"
        if usage.on_demand_enabled and usage.on_demand_used:
            body += f"  od:{usage.on_demand_used:.0f}"
    else:
        percent = usage.percent
        bar = _bar(percent)
        pct = f" {percent:.0f}%" if percent is not None else ""
        body = f"{bar}{pct}"
        if usage.on_demand_enabled and usage.on_demand_used is not None:
            body += f"  od:{usage.on_demand_used:.0f}"
    age = _age_label(usage.fetched_at)
    if stale and age:
        return f"{body} [dim]· {age}[/]"
    return body


def _fmt_pct(percent: float) -> str:
    color = "green" if percent < 70 else "yellow" if percent < 90 else "red"
    return f"[{color}]{percent:.0f}%[/]"


def _age_label(fetched_at: str | None) -> str:
    if not fetched_at:
        return ""
    try:
        stamp = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    seconds = int((datetime.now(timezone.utc) - stamp).total_seconds())
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _reset_cell(usage: UsageSnapshot | None) -> str:
    if not usage or not usage.billing_cycle_end:
        return "-"
    try:
        end = datetime.fromisoformat(usage.billing_cycle_end.replace("Z", "+00:00"))
    except ValueError:
        return usage.billing_cycle_end[:10]
    now = datetime.now(timezone.utc)
    delta = end - now
    if delta.total_seconds() <= 0:
        return "now"
    days = int(delta.total_seconds() // 86400)
    hours = int((delta.total_seconds() % 86400) // 3600)
    if days > 0:
        return f"{days}d {hours}h"
    return f"{hours}h"


def _bar(percent: float | None, width: int = 8) -> str:
    if percent is None:
        return "[dim]n/a[/]"
    clamped = max(0.0, min(100.0, percent))
    filled = int(round((clamped / 100) * width))
    empty = width - filled
    color = "green" if clamped < 70 else "yellow" if clamped < 90 else "red"
    if _supports_unicode():
        block, vacant = "█", "░"
    else:
        block, vacant = "#", "-"
    return f"[{color}]{block * filled}{vacant * empty}[/]"
