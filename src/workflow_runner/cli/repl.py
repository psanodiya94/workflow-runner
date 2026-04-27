"""Interactive REPL.

The REPL holds a :class:`SessionManager` and lets the operator open / close
SSH sessions, run ad-hoc commands, drive workflows in linear or step mode,
and inspect status. Commands prefixed with ``!`` are passed straight through
to the active session so a typo on a meta-command doesn't accidentally land
on the remote host.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from rich.console import Console

from workflow_runner.cli.console import (
    make_console,
    render_result,
    render_status,
    render_workflow_summary,
)
from workflow_runner.connection.manager import ConnectionConfig, SessionManager
from workflow_runner.debugger.stepper import WorkflowDebugger
from workflow_runner.execution.executor import CommandExecutor
from workflow_runner.logging_utils import get_logger
from workflow_runner.security.guard import SecurityGuard, SecurityVerdict, Severity
from workflow_runner.workflow.engine import WorkflowEngine
from workflow_runner.workflow.loader import WorkflowLoadError, load_workflow

_META_COMMANDS = (
    "help", "status", "connect", "disconnect", "use", "sessions",
    "run", "workflow", "debug", "exit", "quit",
)


@dataclass
class _ReplState:
    active_session: str | None = None


class WorkflowRunnerRepl:
    """The interactive shell."""

    def __init__(
        self,
        *,
        sessions: SessionManager,
        guard: SecurityGuard | None = None,
        history_file: Path | None = None,
    ) -> None:
        self._sessions = sessions
        self._guard = guard or SecurityGuard()
        self._console: Console = make_console()
        self._state = _ReplState()
        self._log = get_logger("workflow_runner.cli")
        completer = WordCompleter(list(_META_COMMANDS), ignore_case=True)
        history = FileHistory(str(history_file)) if history_file else None
        self._prompt = PromptSession(completer=completer, history=history)

    # --------------------------------------------------------------- lifecycle
    def run(self) -> int:
        self._console.print(
            "[bold cyan]workflow-runner[/bold cyan] — type [bold]help[/bold] for commands, "
            "[bold]exit[/bold] to quit."
        )
        try:
            while True:
                try:
                    raw = self._prompt.prompt(self._prompt_text())
                except KeyboardInterrupt:
                    self._console.print("[yellow](use 'exit' to quit)[/yellow]")
                    continue
                except EOFError:
                    break
                if raw is None:
                    break
                line = raw.strip()
                if not line:
                    continue
                if not self._dispatch(line):
                    break
        finally:
            self._sessions.disconnect_all()
        return 0

    # ----------------------------------------------------------------- dispatch
    def _prompt_text(self) -> str:
        if self._state.active_session is None:
            return "tool> "
        return f"session({self._state.active_session})> "

    def _dispatch(self, line: str) -> bool:
        if line.startswith("!"):
            self._handle_run_passthrough(line[1:].strip())
            return True
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError as exc:
            self._console.print(f"[red]parse error:[/red] {exc}")
            return True
        if not tokens:
            return True
        cmd, args = tokens[0].lower(), tokens[1:]
        handler = {
            "help": self._cmd_help,
            "status": self._cmd_status,
            "sessions": self._cmd_status,
            "connect": self._cmd_connect,
            "disconnect": self._cmd_disconnect,
            "use": self._cmd_use,
            "run": self._cmd_run,
            "workflow": self._cmd_workflow,
            "debug": self._cmd_debug,
            "exit": self._cmd_exit,
            "quit": self._cmd_exit,
        }.get(cmd)
        if handler is None:
            self._console.print(
                f"[red]unknown command:[/red] {cmd}. Type 'help' or prefix with '!' to run remotely."
            )
            return True
        try:
            return handler(args)
        except Exception as exc:  # pragma: no cover - last-resort UX safety net
            self._log.exception("command crashed: %s", cmd)
            self._console.print(f"[red]error:[/red] {exc}")
            return True

    # --------------------------------------------------------------- handlers
    def _cmd_help(self, _args: list[str]) -> bool:
        self._console.print(
            """
[bold]Available commands[/bold]

  [cyan]help[/cyan]                          — show this help
  [cyan]status[/cyan] | [cyan]sessions[/cyan]            — list registered sessions and state
  [cyan]connect[/cyan] <name> [opts]         — open an SSH session (see below)
  [cyan]disconnect[/cyan] <name>             — close and remove a session
  [cyan]use[/cyan] <name>                    — make <name> the active session
  [cyan]run[/cyan] <command...>              — run an ad-hoc command on the active session
  [cyan]![/cyan]<command...>                 — shorthand for [cyan]run[/cyan]
  [cyan]workflow[/cyan] <path>               — execute a workflow on the active session
  [cyan]debug[/cyan] <path>                  — step through a workflow (gdb-style)
  [cyan]exit[/cyan] | [cyan]quit[/cyan]                 — disconnect everything and leave

[bold]connect options[/bold]
  --host <host>            (required)
  --user <user>            (required)
  --port <int>             (default 22)
  --identity <key-file>    SSH private key path
  --password               prompt for a password
  --no-agent               do not use ssh-agent
  --insecure               disable strict host-key checking (development only)
  --local                  use a local subprocess instead of SSH

[bold]debug commands[/bold] (inside a debug session)
  next | n                 — execute the next step
  continue | c             — execute every remaining step
  prev | p                 — re-show the previous step's output
  list | l                 — show step list and cursor
  stop | q                 — abort the workflow
            """.strip()
        )
        return True

    def _cmd_status(self, _args: list[str]) -> bool:
        render_status(self._console, self._sessions.status())
        if self._state.active_session:
            self._console.print(f"[bold]active:[/bold] {self._state.active_session}")
        return True

    def _cmd_connect(self, args: list[str]) -> bool:
        if not args:
            self._console.print("[red]usage:[/red] connect <name> --host H --user U [opts]")
            return True
        name = args[0]
        opts = _parse_connect_options(args[1:])
        if opts is None:
            self._console.print("[red]invalid options[/red]")
            return True

        password: str | None = None
        if opts.prompt_password:
            try:
                from getpass import getpass

                password = getpass(f"Password for {opts.username}@{opts.host}: ")
            except KeyboardInterrupt:
                self._console.print("[yellow]password prompt cancelled[/yellow]")
                return True

        config = ConnectionConfig(
            name=name,
            host=opts.host,
            port=opts.port,
            username=opts.username,
            password=password,
            key_filename=opts.identity,
            use_agent=opts.use_agent,
            strict_host_key_checking=not opts.insecure,
            local=opts.local,
        )
        try:
            connection = self._sessions.add(config, connect=True)
        except Exception as exc:
            self._console.print(f"[red]connect failed:[/red] {exc}")
            return True
        self._state.active_session = name
        self._console.print(f"[green]connected[/green] {name} -> {connection.describe()}")
        return True

    def _cmd_disconnect(self, args: list[str]) -> bool:
        target = args[0] if args else self._state.active_session
        if not target:
            self._console.print("[red]nothing to disconnect (no active session)[/red]")
            return True
        try:
            self._sessions.remove(target)
        except KeyError:
            self._console.print(f"[red]unknown session:[/red] {target}")
            return True
        if self._state.active_session == target:
            self._state.active_session = None
        self._console.print(f"[green]disconnected[/green] {target}")
        return True

    def _cmd_use(self, args: list[str]) -> bool:
        if not args:
            self._console.print("[red]usage:[/red] use <name>")
            return True
        name = args[0]
        try:
            self._sessions.get(name)
        except KeyError:
            self._console.print(f"[red]unknown session:[/red] {name}")
            return True
        self._state.active_session = name
        self._console.print(f"[green]active session ->[/green] {name}")
        return True

    def _cmd_run(self, args: list[str]) -> bool:
        if not args:
            self._console.print("[red]usage:[/red] run <command...>")
            return True
        return self._handle_run_passthrough(" ".join(args))

    def _handle_run_passthrough(self, command: str) -> bool:
        executor = self._require_executor()
        if executor is None:
            return True
        result = executor.run(command, stream=self._stream_handler())
        if result.stdout or result.stderr or result.error:
            render_result(self._console, result)
        else:
            render_result(self._console, result)
        return True

    def _cmd_workflow(self, args: list[str]) -> bool:
        if not args:
            self._console.print("[red]usage:[/red] workflow <path>")
            return True
        executor = self._require_executor()
        if executor is None:
            return True
        try:
            workflow = load_workflow(args[0])
        except WorkflowLoadError as exc:
            self._console.print(f"[red]workflow load error:[/red] {exc}")
            return True
        engine = WorkflowEngine(workflow, executor, stream=self._stream_handler())
        report = engine.run_all()
        render_workflow_summary(self._console, report)
        return True

    def _cmd_debug(self, args: list[str]) -> bool:
        if not args:
            self._console.print("[red]usage:[/red] debug <path>")
            return True
        executor = self._require_executor()
        if executor is None:
            return True
        try:
            workflow = load_workflow(args[0])
        except WorkflowLoadError as exc:
            self._console.print(f"[red]workflow load error:[/red] {exc}")
            return True
        engine = WorkflowEngine(workflow, executor, stream=self._stream_handler())
        debugger = WorkflowDebugger(engine)
        self._run_debugger_loop(debugger, workflow.name)
        return True

    def _cmd_exit(self, _args: list[str]) -> bool:
        self._console.print("bye.")
        return False

    # --------------------------------------------------------------- helpers
    def _require_executor(self) -> CommandExecutor | None:
        if self._state.active_session is None:
            self._console.print("[red]no active session.[/red] Use 'connect' or 'use'.")
            return None
        try:
            connection = self._sessions.ensure_alive(self._state.active_session)
        except Exception as exc:
            self._console.print(f"[red]session unavailable:[/red] {exc}")
            return None
        return CommandExecutor(
            connection,
            guard=self._guard,
            confirm=self._confirm_destructive,
            logger_context={"session": self._state.active_session},
        )

    def _stream_handler(self):
        console = self._console

        def _stream(channel: str, data: str) -> None:
            style = "default" if channel == "stdout" else "red"
            console.print(data, end="", style=style, soft_wrap=True, highlight=False)

        return _stream

    def _confirm_destructive(self, command: str, verdict: SecurityVerdict) -> bool:
        marker = "DANGEROUS" if verdict.severity is Severity.DANGEROUS else "CAUTION"
        self._console.print(
            f"[bold red]{marker}[/bold red]: {command}\n"
            f"  reasons: {', '.join(verdict.reasons)}"
        )
        try:
            answer = self._prompt.prompt("type 'yes' to proceed: ")
        except (KeyboardInterrupt, EOFError):
            return False
        return answer.strip().lower() in {"y", "yes"}

    def _run_debugger_loop(self, debugger: WorkflowDebugger, workflow_name: str) -> None:
        self._console.print(
            f"[bold]debug:[/bold] {workflow_name}  ({len(debugger._engine.workflow)} steps)"
        )
        completer = WordCompleter(
            ["next", "n", "continue", "c", "prev", "p", "list", "l", "stop", "q"],
            ignore_case=True,
        )
        prompt = PromptSession(completer=completer)
        while not debugger.is_done:
            peek = debugger.peek()
            if peek is not None:
                self._console.print(
                    f"[bold cyan]>[/bold cyan] [step {debugger.cursor + 1}/"
                    f"{len(debugger._engine.workflow)}] "
                    f"[bold]{peek.name}[/bold]: {peek.command}"
                )
            try:
                cmd = prompt.prompt("workflow(debug)> ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                self._console.print("[yellow]aborting.[/yellow]")
                debugger.stop()
                break
            if cmd in {"next", "n", ""}:
                executed = debugger.step()
                if executed is None:
                    self._console.print("[dim]workflow finished.[/dim]")
                    break
                render_result(self._console, executed.result, title=f"step {executed.index + 1}: {executed.name}")
            elif cmd in {"continue", "c"}:
                debugger.continue_remaining()
                self._console.print("[dim]continued to end.[/dim]")
                break
            elif cmd in {"prev", "p"}:
                last = debugger.previous()
                if last is None:
                    self._console.print("[dim]no previous step.[/dim]")
                else:
                    render_result(
                        self._console,
                        last.result,
                        title=f"(replay) step {last.index + 1}: {last.name}",
                    )
            elif cmd in {"list", "l"}:
                self._render_step_list(debugger)
            elif cmd in {"stop", "q"}:
                debugger.stop()
                self._console.print("[yellow]workflow stopped.[/yellow]")
                break
            else:
                self._console.print(f"[red]unknown debug command:[/red] {cmd}")
        render_workflow_summary(self._console, debugger.report)

    def _render_step_list(self, debugger: WorkflowDebugger) -> None:
        from rich.table import Table

        table = Table(title="Steps")
        table.add_column("#", justify="right")
        table.add_column("Name", style="cyan")
        table.add_column("Status")
        table.add_column("Command")
        statuses = {h.index: h.result.status.value for h in debugger.history}
        for i, step in enumerate(debugger._engine.workflow.steps):
            status = statuses.get(i, "[dim]pending[/dim]")
            marker = " (next)" if i == debugger.cursor else ""
            table.add_row(str(i + 1), step.name + marker, status, step.command)
        self._console.print(table)


# --------------------------------------------------------------------- options
@dataclass
class _ConnectOptions:
    host: str = ""
    username: str = ""
    port: int = 22
    identity: str | None = None
    use_agent: bool = True
    insecure: bool = False
    prompt_password: bool = False
    local: bool = False


def _parse_connect_options(tokens: list[str]) -> _ConnectOptions | None:
    """Parse the ``connect`` subcommand's option list.

    Supports a small flag dialect mirroring the ``ssh`` command. Returns None
    on malformed input (caller prints usage).
    """
    opts = _ConnectOptions()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--host" and i + 1 < len(tokens):
            opts.host = tokens[i + 1]
            i += 2
        elif tok in ("--user", "--username") and i + 1 < len(tokens):
            opts.username = tokens[i + 1]
            i += 2
        elif tok == "--port" and i + 1 < len(tokens):
            try:
                opts.port = int(tokens[i + 1])
            except ValueError:
                return None
            i += 2
        elif tok in ("--identity", "-i") and i + 1 < len(tokens):
            opts.identity = tokens[i + 1]
            i += 2
        elif tok == "--password":
            opts.prompt_password = True
            i += 1
        elif tok == "--no-agent":
            opts.use_agent = False
            i += 1
        elif tok == "--insecure":
            opts.insecure = True
            i += 1
        elif tok == "--local":
            opts.local = True
            i += 1
        else:
            return None
    if not opts.local and (not opts.host or not opts.username):
        return None
    return opts
