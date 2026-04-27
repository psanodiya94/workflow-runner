"""Rich-based console wrappers for consistent CLI output."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from workflow_runner.execution.result import CommandResult, ExecutionStatus

_STATUS_STYLE = {
    ExecutionStatus.SUCCESS: "bold green",
    ExecutionStatus.FAILURE: "bold red",
    ExecutionStatus.TIMEOUT: "bold yellow",
    ExecutionStatus.ABORTED: "bold magenta",
    ExecutionStatus.SKIPPED: "dim",
    ExecutionStatus.BLOCKED: "bold red on white",
}


def make_console() -> Console:
    return Console(highlight=False, soft_wrap=False)


def render_status(console: Console, sessions: dict[str, dict[str, str]]) -> None:
    if not sessions:
        console.print("[dim]No active sessions.[/dim]")
        return
    table = Table(title="Sessions", show_lines=False)
    table.add_column("Name", style="cyan")
    table.add_column("Target")
    table.add_column("State")
    table.add_column("Alive")
    for name, info in sessions.items():
        state_style = "green" if info.get("alive") == "yes" else "red"
        table.add_row(
            name,
            info.get("target", "?"),
            Text(info.get("state", "?"), style=state_style),
            info.get("alive", "?"),
        )
    console.print(table)


def render_result(console: Console, result: CommandResult, *, title: str | None = None) -> None:
    style = _STATUS_STYLE.get(result.status, "white")
    summary = (
        f"exit={result.exit_code}  status={result.status.value}  "
        f"duration={result.duration:.3f}s"
    )
    body_parts: list[str] = []
    if result.stdout:
        body_parts.append("[bold]stdout[/bold]\n" + result.stdout.rstrip())
    if result.stderr:
        body_parts.append("[bold]stderr[/bold]\n" + result.stderr.rstrip())
    if result.error:
        body_parts.append(f"[bold red]error:[/bold red] {result.error}")
    body = "\n\n".join(body_parts) or "[dim](no output)[/dim]"
    panel_title = title or f"$ {result.command}"
    console.print(
        Panel(
            body,
            title=panel_title,
            subtitle=Text(summary, style=style),
            border_style=style,
        )
    )


def render_workflow_summary(console: Console, report) -> None:
    table = Table(title=f"Workflow report: {report.workflow}")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Total steps", str(report.total))
    table.add_row("Succeeded", str(report.succeeded))
    table.add_row("Failed", str(report.failed))
    table.add_row("Skipped", str(report.skipped))
    table.add_row("Aborted", str(report.aborted))
    table.add_row("Blocked", str(report.blocked))
    table.add_row("Overall", "[green]OK[/green]" if report.ok else "[red]FAILED[/red]")
    console.print(table)
