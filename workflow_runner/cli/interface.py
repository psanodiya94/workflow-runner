"""Click-based CLI — entry point for all user-facing commands."""

from __future__ import annotations

import re
import sys
from typing import Optional

import click

from workflow_runner.cli.formatter import (
    console,
    err_console,
    print_debugger_result,
    print_debugger_step,
    print_debugger_step_list,
    print_step_banner,
    print_step_outcome,
    print_workflow_summary,
    print_session_table,
)
from workflow_runner.connection.manager import SessionManager
from workflow_runner.connection.session import SessionConfig
from workflow_runner.executor.command import is_destructive
from workflow_runner.logger import set_verbosity
from workflow_runner.workflow.engine import WorkflowEngine
from workflow_runner.workflow.loader import load_workflow

# Global session pool shared across all commands within one process invocation
_manager = SessionManager()


# ──────────────────────────────────────────────────────────────────────────────
# Root group
# ──────────────────────────────────────────────────────────────────────────────

@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable DEBUG-level logging to console.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """
    wfr — remote command execution and workflow automation.

    \b
    Quick-start:
      wfr shell user@host              Interactive shell
      wfr run   workflow.yaml user@host  Run a workflow
      wfr debug workflow.yaml user@host  Step-by-step debugger
    """
    ctx.ensure_object(dict)
    ctx.obj["manager"] = _manager
    set_verbosity(verbose)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _parse_host(host_str: str) -> tuple[str, str, int]:
    """
    Parse ``[user@]host[:port]`` into ``(username, host, port)``.

    Returns an empty string for username when not supplied
    (``SessionConfig`` will fall back to the OS user).
    """
    m = re.match(r"^(?:([^@]+)@)?([^:]+)(?::(\d+))?$", host_str)
    if not m:
        raise click.BadParameter(
            f"Cannot parse '{host_str}'. Expected [user@]host[:port]."
        )
    return (m.group(1) or "", m.group(2), int(m.group(3) or 22))


def _build_session(
    manager: SessionManager,
    host_str: str,
    key: Optional[str],
    use_password: bool,
    session_id: Optional[str],
    timeout: float,
) -> tuple[str, "Session"]:  # type: ignore[name-defined]
    """Connect a new session and return (session_id, session)."""
    from workflow_runner.connection.session import Session

    username, host, port = _parse_host(host_str)
    sid = session_id or f"{username}@{host}" if username else host

    pw: Optional[str] = None
    if use_password:
        import getpass
        pw = getpass.getpass(f"Password for {username or 'user'}@{host}: ")

    config = SessionConfig(
        host=host,
        port=port,
        username=username,
        key_path=key,
        password=pw,
        timeout=timeout,
    )

    session = manager.create(sid, config)
    console.print(
        f"[yellow]Connecting to[/yellow] [bold]{session.label}[/bold]…"
    )
    session.connect()
    console.print(
        f"[green]✓ Connected[/green]  session=[bold]{sid}[/bold]"
    )
    return sid, session


# ──────────────────────────────────────────────────────────────────────────────
# shell
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("shell")
@click.argument("host")
@click.option("--key", "-i", default=None, metavar="PATH", help="SSH private key file.")
@click.option("--password", "use_password", is_flag=True, help="Prompt for SSH password.")
@click.option("--session-id", "-s", default=None, help="Label for this session.")
@click.option("--timeout", default=30.0, show_default=True, help="Connection timeout (seconds).")
@click.pass_context
def shell(
    ctx: click.Context,
    host: str,
    key: Optional[str],
    use_password: bool,
    session_id: Optional[str],
    timeout: float,
) -> None:
    """Connect to HOST and enter an interactive shell.

    \b
    HOST format:  [user@]host[:port]
    Examples:
      wfr shell admin@10.0.0.5
      wfr shell deploy@myserver.example.com:2222 -i ~/.ssh/deploy_key
    """
    manager: SessionManager = ctx.obj["manager"]
    try:
        sid, _ = _build_session(manager, host, key, use_password, session_id, timeout)
    except Exception as exc:
        err_console.print(f"[red]Connection failed: {exc}[/red]")
        sys.exit(1)

    from workflow_runner.cli.repl import InteractiveRepl
    InteractiveRepl(manager).run(sid)
    manager.remove(sid)


# ──────────────────────────────────────────────────────────────────────────────
# run
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("run")
@click.argument("workflow_file", type=click.Path(exists=True))
@click.argument("host")
@click.option("--key", "-i", default=None, metavar="PATH", help="SSH private key file.")
@click.option("--password", "use_password", is_flag=True, help="Prompt for SSH password.")
@click.option("--session-id", "-s", default=None, help="Label for this session.")
@click.option("--timeout", default=30.0, show_default=True, help="Connection timeout (seconds).")
@click.pass_context
def run_workflow(
    ctx: click.Context,
    workflow_file: str,
    host: str,
    key: Optional[str],
    use_password: bool,
    session_id: Optional[str],
    timeout: float,
) -> None:
    """Run WORKFLOW_FILE on HOST, then disconnect.

    \b
    WORKFLOW_FILE: path to a .yaml, .json, or .py workflow definition.
    HOST format:   [user@]host[:port]

    \b
    Examples:
      wfr run system_check.yaml admin@10.0.0.5
      wfr run deploy.yaml deploy@prod.example.com -i ~/.ssh/id_ed25519
    """
    manager: SessionManager = ctx.obj["manager"]

    try:
        workflow = load_workflow(workflow_file)
    except Exception as exc:
        err_console.print(f"[red]Failed to load workflow: {exc}[/red]")
        sys.exit(1)

    console.print(
        f"\n[bold]Workflow:[/bold] {workflow.name}"
        f"  [dim]v{workflow.version}[/dim]\n"
        f"[dim]{workflow.description}[/dim]\n"
        f"[dim]Steps: {len(workflow.steps)}[/dim]\n"
    )

    try:
        sid, session = _build_session(manager, host, key, use_password, session_id, timeout)
    except Exception as exc:
        err_console.print(f"[red]Connection failed: {exc}[/red]")
        sys.exit(1)

    engine = WorkflowEngine(session)

    def _on_confirm(step) -> bool:
        return click.confirm(
            f"⚠  Destructive command: [{step.command}] — execute?", default=False
        )

    try:
        wf_run = engine.run(
            workflow,
            on_step_start=lambda i, s: print_step_banner(i, len(workflow.steps), s),
            on_step_done=lambda i, sr: print_step_outcome(sr),
            on_confirm=_on_confirm,
            on_stdout=lambda chunk: console.print(chunk, end=""),
            on_stderr=lambda chunk: err_console.print(chunk, end="", style="red"),
        )
    finally:
        manager.remove(sid)

    print_workflow_summary(wf_run)

    if wf_run.status.value != "completed":
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# debug
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("debug")
@click.argument("workflow_file", type=click.Path(exists=True))
@click.argument("host")
@click.option("--key", "-i", default=None, metavar="PATH", help="SSH private key file.")
@click.option("--password", "use_password", is_flag=True, help="Prompt for SSH password.")
@click.option("--session-id", "-s", default=None, help="Label for this session.")
@click.option("--timeout", default=30.0, show_default=True, help="Connection timeout (seconds).")
@click.pass_context
def debug_workflow(
    ctx: click.Context,
    workflow_file: str,
    host: str,
    key: Optional[str],
    use_password: bool,
    session_id: Optional[str],
    timeout: float,
) -> None:
    """Step-by-step workflow debugger (gdb-like).

    \b
    Debugger commands:
      next  / n    Execute next step
      continue / c Execute all remaining steps
      stop  / s    Abort the workflow
      back  / b    Show previous step's output
      list  / l    List all steps with execution status
      help  / h    Show this help

    \b
    WORKFLOW_FILE: path to a .yaml, .json, or .py workflow definition.
    HOST format:   [user@]host[:port]
    """
    manager: SessionManager = ctx.obj["manager"]

    try:
        workflow = load_workflow(workflow_file)
    except Exception as exc:
        err_console.print(f"[red]Failed to load workflow: {exc}[/red]")
        sys.exit(1)

    console.print(
        f"\n[bold magenta]Debugger:[/bold magenta] {workflow.name}"
        f"  [dim]v{workflow.version}[/dim]\n"
        f"[dim]{workflow.description}[/dim]\n"
        f"[dim]{len(workflow.steps)} steps — "
        "commands: next|n, continue|c, stop|s, back|b, list|l, help|h[/dim]\n"
    )

    try:
        sid, session = _build_session(manager, host, key, use_password, session_id, timeout)
    except Exception as exc:
        err_console.print(f"[red]Connection failed: {exc}[/red]")
        sys.exit(1)

    engine = WorkflowEngine(session)
    dbg = engine.create_debugger(workflow)

    def _confirm(step) -> bool:
        return click.confirm(
            f"⚠  Destructive command: [{step.command}] — execute?", default=False
        )

    dbg.on_confirm = _confirm
    dbg.start()

    from prompt_toolkit import PromptSession as _PSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import HTML

    dbg_prompt: _PSession = _PSession(
        completer=WordCompleter(
            ["next", "n", "continue", "c", "stop", "s", "back", "b", "list", "l", "help", "h"],
            ignore_case=True,
        ),
        complete_while_typing=False,
    )

    _print_debugger_help()

    try:
        while not dbg.is_done:
            step = dbg.current_step
            if step is None:
                break

            print_debugger_step(dbg.current_index, dbg.total_steps, step)

            try:
                raw = dbg_prompt.prompt(
                    HTML(
                        "<ansimagenta>debugger</ansimagenta>"
                        " <ansigreen>❯</ansigreen> "
                    )
                ).strip().lower()
            except (KeyboardInterrupt, EOFError):
                dbg.step_abort()
                console.print("\n[yellow]Workflow aborted.[/yellow]")
                break

            if raw in ("next", "n", ""):
                sr = dbg.step_next()
                if sr:
                    print_debugger_result(dbg.current_index - 1, sr)

            elif raw in ("continue", "c"):
                console.print("[dim]Continuing all remaining steps…[/dim]")
                while not dbg.is_done:
                    step = dbg.current_step
                    if step is None:
                        break
                    print_debugger_step(dbg.current_index, dbg.total_steps, step)
                    sr = dbg.step_next()
                    if sr:
                        print_debugger_result(dbg.current_index - 1, sr)

            elif raw in ("stop", "s"):
                dbg.step_abort()
                console.print("[yellow]Workflow aborted.[/yellow]")

            elif raw in ("back", "b"):
                idx = dbg.current_index - 1
                prev = dbg.get_step_result(idx)
                if prev:
                    console.print(f"[dim]← Step {idx + 1}: {prev.step.name}[/dim]")
                    print_debugger_result(idx, prev)
                else:
                    console.print("[dim]No previous step.[/dim]")

            elif raw in ("list", "l"):
                print_debugger_step_list(workflow.steps, dbg.current_index)

            elif raw in ("help", "h"):
                _print_debugger_help()

            else:
                console.print(
                    f"[dim]Unknown command '{raw}'. Type [cyan]help[/cyan].[/dim]"
                )
    finally:
        manager.remove(sid)

    print_workflow_summary(dbg.run)

    if dbg.run.status.value not in ("completed",):
        sys.exit(1)


def _print_debugger_help() -> None:
    console.print(
        "\n[bold]Debugger Commands[/bold]\n"
        "  [magenta]next[/magenta]     / [magenta]n[/magenta]   Execute next step\n"
        "  [magenta]continue[/magenta] / [magenta]c[/magenta]   Execute all remaining steps without pausing\n"
        "  [magenta]stop[/magenta]     / [magenta]s[/magenta]   Abort the workflow\n"
        "  [magenta]back[/magenta]     / [magenta]b[/magenta]   Show previous step's output\n"
        "  [magenta]list[/magenta]     / [magenta]l[/magenta]   List all steps with current status\n"
        "  [magenta]help[/magenta]     / [magenta]h[/magenta]   Show this help\n"
        "  [dim]<Enter>[/dim]               Same as next\n"
    )
