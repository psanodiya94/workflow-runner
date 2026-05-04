"""argparse-based entry point.

Subcommands:

* ``interactive`` (default) — start the REPL with optional pre-connected session
* ``run`` — execute a single command on a host and exit
* ``workflow`` — execute a workflow file and exit
* ``debug`` — load a workflow into the step-by-step debugger

All subcommands share the same connection flags so muscle memory transfers
between them.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from workflow_runner import __version__
from workflow_runner.cli.console import (
    make_console,
    render_result,
    render_workflow_summary,
)
from workflow_runner.cli.repl import WorkflowRunnerRepl
from workflow_runner.connection.manager import ConnectionConfig, SessionManager
from workflow_runner.debugger.stepper import WorkflowDebugger
from workflow_runner.execution.executor import CommandExecutor
from workflow_runner.execution.result import ExecutionStatus
from workflow_runner.logging_utils import configure_logging, get_logger
from workflow_runner.security.guard import SecurityGuard, SecurityVerdict
from workflow_runner.workflow.engine import WorkflowEngine
from workflow_runner.workflow.loader import WorkflowLoadError, load_workflow

DEFAULT_HISTORY = Path.home() / ".workflow_runner_history"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    log_file = Path(args.log_file).expanduser() if args.log_file else None
    configure_logging(level=args.log_level.upper(), log_file=log_file, json_console=args.log_json)
    log = get_logger("workflow_runner.cli", subcommand=args.subcommand or "interactive")
    log.info("workflow-runner %s starting", __version__)

    try:
        if args.subcommand == "interactive":
            return _run_interactive(args)
        if args.subcommand == "run":
            return _run_oneshot(args)
        if args.subcommand == "workflow":
            return _run_workflow(args)
        if args.subcommand == "debug":
            return _run_debug(args)
        if args.subcommand == None:
            parser.print_help()
    except KeyboardInterrupt:
        return 130
    parser.error(f"unknown subcommand: {args.subcommand}")
    return 2


# --------------------------------------------------------------------- parser
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workflow-runner",
        description="Persistent remote command execution and workflow runner.",
    )
    parser.add_argument("--version", action="version", version=f"workflow-runner {__version__}")
    parser.add_argument("--log-level", default="INFO", help="DEBUG|INFO|WARNING|ERROR")
    parser.add_argument("--log-file", help="path to a JSONL log file")
    parser.add_argument("--log-json", action="store_true", help="emit JSON on the console too")

    sub = parser.add_subparsers(dest="subcommand")

    p_interactive = sub.add_parser("interactive", help="start the REPL")
    _add_optional_connection_args(p_interactive)
    p_interactive.add_argument(
        "--history",
        default=str(DEFAULT_HISTORY),
        help=f"history file path (default: {DEFAULT_HISTORY})",
    )

    p_run = sub.add_parser("run", help="run a single command on a host")
    _add_required_connection_args(p_run)
    p_run.add_argument("command", nargs=argparse.REMAINDER, help="command to execute")

    p_workflow = sub.add_parser("workflow", help="execute a workflow file")
    _add_required_connection_args(p_workflow)
    p_workflow.add_argument("workflow", help="path to workflow YAML/JSON")

    p_debug = sub.add_parser("debug", help="step through a workflow interactively")
    _add_required_connection_args(p_debug)
    p_debug.add_argument("workflow", help="path to workflow YAML/JSON")

    return parser


def _add_required_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host")
    parser.add_argument("--user")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("-i", "--identity", help="SSH private key file")
    parser.add_argument("--password", action="store_true", help="prompt for SSH password")
    parser.add_argument("--no-agent", action="store_true", help="disable ssh-agent")
    parser.add_argument("--insecure", action="store_true", help="disable strict host key check")
    parser.add_argument("--local", action="store_true", help="run against local subprocess")
    parser.add_argument("--name", default="default", help="session name (default: 'default')")
    parser.add_argument("--timeout", type=float, help="per-command timeout in seconds")


def _add_optional_connection_args(parser: argparse.ArgumentParser) -> None:
    _add_required_connection_args(parser)


# --------------------------------------------------------------------- shared
def _build_session_from_args(args: argparse.Namespace) -> tuple[SessionManager, str]:
    sessions = SessionManager()
    if not args.local and not args.host:
        return sessions, ""  # caller will report
    password = None
    if args.password and not args.local:
        password = getpass.getpass(f"Password for {args.user}@{args.host}: ")
    config = ConnectionConfig(
        name=args.name,
        host=args.host,
        port=args.port,
        username=args.user,
        password=password,
        key_filename=args.identity,
        use_agent=not args.no_agent,
        strict_host_key_checking=not args.insecure,
        local=args.local,
    )
    sessions.add(config, connect=True)
    return sessions, args.name


def _confirm_destructive_console(console) -> callable:
    def _confirm(command: str, verdict: SecurityVerdict) -> bool:
        console.print(f"[bold red]DESTRUCTIVE COMMAND[/bold red]: {command}")
        console.print(f"  reasons: {', '.join(verdict.reasons)}")
        try:
            answer = input("type 'yes' to proceed: ")
        except (KeyboardInterrupt, EOFError):
            return False
        return answer.strip().lower() in {"y", "yes"}

    return _confirm


# --------------------------------------------------------------- subcommands
def _run_interactive(args: argparse.Namespace) -> int:
    sessions = SessionManager()
    if args.local or args.host:
        sessions, _ = _build_session_from_args(args)
    repl = WorkflowRunnerRepl(
        sessions=sessions,
        guard=SecurityGuard(),
        history_file=Path(args.history) if args.history else None,
    )
    return repl.run()


def _run_oneshot(args: argparse.Namespace) -> int:
    if not args.command:
        print("error: no command supplied", file=sys.stderr)
        return 2
    if not args.local and not args.host:
        print("error: --host (and --user) required (or use --local)", file=sys.stderr)
        return 2
    console = make_console()
    sessions, name = _build_session_from_args(args)
    try:
        connection = sessions.get(name)
        executor = CommandExecutor(
            connection,
            guard=SecurityGuard(),
            default_timeout=args.timeout,
            confirm=_confirm_destructive_console(console),
        )
        cmd = " ".join(args.command).strip()
        result = executor.run(cmd, stream=_make_console_stream(console))
        render_result(console, result)
        return 0 if result.status is ExecutionStatus.SUCCESS else 1
    finally:
        sessions.disconnect_all()


def _run_workflow(args: argparse.Namespace) -> int:
    if not args.local and not args.host:
        print("error: --host (and --user) required (or use --local)", file=sys.stderr)
        return 2
    console = make_console()
    try:
        workflow = load_workflow(args.workflow)
    except WorkflowLoadError as exc:
        print(f"workflow load error: {exc}", file=sys.stderr)
        return 2
    sessions, name = _build_session_from_args(args)
    try:
        executor = CommandExecutor(
            sessions.get(name),
            guard=SecurityGuard(),
            default_timeout=args.timeout,
            confirm=_confirm_destructive_console(console),
        )
        engine = WorkflowEngine(workflow, executor, stream=_make_console_stream(console))
        report = engine.run_all()
        render_workflow_summary(console, report)
        return 0 if report.ok else 1
    finally:
        sessions.disconnect_all()


def _run_debug(args: argparse.Namespace) -> int:
    if not args.local and not args.host:
        print("error: --host (and --user) required (or use --local)", file=sys.stderr)
        return 2
    console = make_console()
    try:
        workflow = load_workflow(args.workflow)
    except WorkflowLoadError as exc:
        print(f"workflow load error: {exc}", file=sys.stderr)
        return 2
    sessions, name = _build_session_from_args(args)
    try:
        executor = CommandExecutor(
            sessions.get(name),
            guard=SecurityGuard(),
            default_timeout=args.timeout,
            confirm=_confirm_destructive_console(console),
        )
        engine = WorkflowEngine(workflow, executor, stream=_make_console_stream(console))
        debugger = WorkflowDebugger(engine)
        _interactive_debugger(console, debugger)
        return 0 if debugger.report.ok else 1
    finally:
        sessions.disconnect_all()


# ---------------------------------------------------------------- helpers
def _make_console_stream(console):
    def _stream(channel: str, data: str) -> None:
        console.print(data, end="", style="default" if channel == "stdout" else "red", soft_wrap=True, highlight=False)

    return _stream


def _interactive_debugger(console, debugger: WorkflowDebugger) -> None:
    console.print(
        f"[bold]debug:[/bold] {debugger._engine.workflow.name}  "
        f"({len(debugger._engine.workflow)} steps). Commands: next/n, continue/c, prev/p, list/l, stop/q."
    )
    while not debugger.is_done:
        peek = debugger.peek()
        if peek is not None:
            console.print(
                f"[cyan]>[/cyan] [step {debugger.cursor + 1}/{len(debugger._engine.workflow)}] "
                f"[bold]{peek.name}[/bold]: {peek.command}"
            )
        try:
            cmd = input("workflow(debug)> ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            debugger.stop()
            break
        if cmd in {"next", "n", ""}:
            executed = debugger.step()
            if executed is None:
                console.print("[dim]workflow finished.[/dim]")
                break
            render_result(console, executed.result, title=f"step {executed.index + 1}: {executed.name}")
        elif cmd in {"continue", "c"}:
            debugger.continue_remaining()
            break
        elif cmd in {"prev", "p"}:
            last = debugger.previous()
            if last is None:
                console.print("[dim]no previous step.[/dim]")
            else:
                render_result(console, last.result, title=f"(replay) step {last.index + 1}: {last.name}")
        elif cmd in {"list", "l"}:
            from rich.table import Table

            table = Table(title="Steps")
            table.add_column("#", justify="right")
            table.add_column("Name", style="cyan")
            table.add_column("Status")
            table.add_column("Command")
            statuses = {h.index: h.result.status.value for h in debugger.history}
            for i, step in enumerate(debugger._engine.workflow.steps):
                table.add_row(str(i + 1), step.name, statuses.get(i, "pending"), step.command)
            console.print(table)
        elif cmd in {"stop", "q"}:
            debugger.stop()
            break
        else:
            console.print(f"[red]unknown command:[/red] {cmd}")
    render_workflow_summary(console, debugger.report)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
