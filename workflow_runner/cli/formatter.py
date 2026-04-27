"""Rich-based output formatting for every CLI surface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

if TYPE_CHECKING:
    from workflow_runner.connection.session import Session, SessionState
    from workflow_runner.executor.result import CommandResult
    from workflow_runner.workflow.engine import StepResult, WorkflowRun
    from workflow_runner.workflow.models import Step

# Two consoles so stderr output goes to the right stream
console = Console()
err_console = Console(stderr=True)


# ──────────────────────────────────────────────────────────────────────────────
# Session / connection
# ──────────────────────────────────────────────────────────────────────────────

def print_session_table(sessions: dict[str, "Session"], active_id: Optional[str] = None) -> None:
    from workflow_runner.connection.session import SessionState

    _STATE_COLOR = {
        SessionState.CONNECTED: "green",
        SessionState.DISCONNECTED: "red",
        SessionState.CONNECTING: "yellow",
        SessionState.RECONNECTING: "yellow",
        SessionState.ERROR: "red",
    }

    table = Table(title="Sessions", show_header=True, header_style="bold cyan")
    table.add_column("", width=2)
    table.add_column("ID")
    table.add_column("Host")
    table.add_column("State")
    table.add_column("Uptime", justify="right")

    for sid, sess in sessions.items():
        color = _STATE_COLOR.get(sess.state, "white")
        indicator = "→" if sid == active_id else " "
        uptime = f"{sess.uptime:.0f}s" if sess.uptime else "—"
        table.add_row(
            indicator,
            f"[bold]{sid}[/bold]" if sid == active_id else sid,
            sess.label,
            f"[{color}]{sess.state.value}[/{color}]",
            uptime,
        )
    console.print(table)


# ──────────────────────────────────────────────────────────────────────────────
# Command results
# ──────────────────────────────────────────────────────────────────────────────

def print_command_result(result: "CommandResult") -> None:
    """Print exit-code badge and timing; stdout/stderr already streamed live."""
    exit_style = "green" if result.success else "red"
    console.print(
        f"[dim]exit:[/dim] [{exit_style}]{result.exit_code}[/{exit_style}]"
        f"  [dim]({result.execution_time:.2f}s)[/dim]"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Workflow (full-run) display
# ──────────────────────────────────────────────────────────────────────────────

def print_step_banner(index: int, total: int, step: "Step") -> None:
    """Rule + metadata printed before a step starts executing."""
    console.rule(
        f"[bold blue]Step {index + 1}/{total}: {step.name}[/bold blue]",
        style="blue",
    )
    if step.description:
        console.print(f"[dim]{step.description}[/dim]")
    console.print(f"[cyan]$[/cyan] {step.command}")


def print_step_outcome(sr: "StepResult") -> None:
    if sr.skipped:
        console.print("  [yellow]↷ Skipped[/yellow]")
    elif sr.error:
        console.print(f"  [red]✗ Error: {sr.error}[/red]")
    elif sr.result:
        icon = "[green]✓[/green]" if sr.result.success else "[red]✗[/red]"
        console.print(
            f"  {icon} exit={sr.result.exit_code}  time={sr.result.execution_time:.2f}s"
        )


def print_workflow_summary(run: "WorkflowRun") -> None:
    from workflow_runner.workflow.engine import WorkflowStatus

    _STATUS_COLOR = {
        WorkflowStatus.COMPLETED: "green",
        WorkflowStatus.FAILED: "red",
        WorkflowStatus.ABORTED: "yellow",
    }
    color = _STATUS_COLOR.get(run.status, "white")

    table = Table(
        title=f"Workflow: {run.workflow.name}",
        show_header=True,
        header_style="bold",
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Step Name")
    table.add_column("Status", width=8)
    table.add_column("Exit", width=5, justify="right")
    table.add_column("Time", width=8, justify="right")

    for i, sr in enumerate(run.step_results):
        if sr.skipped:
            status_cell = "[yellow]SKIP[/yellow]"
            exit_cell, time_cell = "—", "—"
        elif sr.error:
            status_cell = "[red]ERROR[/red]"
            exit_cell, time_cell = "?", "—"
        elif sr.result:
            if sr.result.success:
                status_cell = "[green]OK[/green]"
            elif sr.step.allow_failure:
                status_cell = "[yellow]WARN[/yellow]"
            else:
                status_cell = "[red]FAIL[/red]"
            exit_cell = str(sr.result.exit_code)
            time_cell = f"{sr.result.execution_time:.2f}s"
        else:
            status_cell = "[dim]pending[/dim]"
            exit_cell, time_cell = "—", "—"

        table.add_row(str(i + 1), sr.step.name, status_cell, exit_cell, time_cell)

    console.print()
    console.print(table)
    console.print(
        f"[bold]Status:[/bold] [{color}]{run.status.value.upper()}[/{color}]"
        f"  [dim]elapsed={run.elapsed:.2f}s"
        f"  steps={len(run.step_results)}/{run.total_steps}[/dim]"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Debugger display
# ──────────────────────────────────────────────────────────────────────────────

def print_debugger_step(index: int, total: int, step: "Step") -> None:
    console.rule(
        f"[bold magenta]► Step {index + 1}/{total}: {step.name}[/bold magenta]",
        style="magenta",
    )
    if step.description:
        console.print(f"  [dim]{step.description}[/dim]")
    console.print(f"  [cyan]Command:[/cyan] {step.command}")
    flags: list[str] = []
    if step.allow_failure:
        flags.append("[yellow]allow_failure[/yellow]")
    if step.confirm_before:
        flags.append("[red]confirm_before[/red]")
    if flags:
        console.print("  " + "  ".join(flags))


def print_debugger_result(index: int, sr: "StepResult") -> None:
    if sr.skipped:
        console.print("  [yellow]↷ Step skipped by user[/yellow]")
        return
    if sr.error:
        console.print(f"  [red]✗ Execution error: {sr.error}[/red]")
        return

    r = sr.result
    exit_style = "green" if r.success else "red"
    console.print(
        f"\n  [bold]Exit:[/bold] [{exit_style}]{r.exit_code}[/{exit_style}]"
        f"  [bold]Time:[/bold] {r.execution_time:.3f}s"
    )
    if r.stdout.strip():
        console.print(
            Panel(r.stdout.rstrip(), title="stdout", border_style="green", padding=(0, 1))
        )
    if r.stderr.strip():
        console.print(
            Panel(r.stderr.rstrip(), title="stderr", border_style="red", padding=(0, 1))
        )


def print_debugger_step_list(steps: list["Step"], current: int) -> None:
    for i, step in enumerate(steps):
        if i < current:
            icon = "[green]✓[/green]"
        elif i == current:
            icon = "[magenta]►[/magenta]"
        else:
            icon = "[dim]○[/dim]"
        console.print(f"  {icon} [{i + 1}] [bold]{step.name}[/bold]: {step.command}")
