"""Interactive REPL — accepts remote Linux commands and streams output live."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory

from workflow_runner.cli.formatter import console, err_console
from workflow_runner.executor.command import is_destructive

if TYPE_CHECKING:
    from workflow_runner.connection.manager import SessionManager


# Common Linux commands offered as completions
_COMMON_COMMANDS = [
    "cat", "cd", "cp", "curl", "date", "df", "du", "echo", "env", "exit",
    "find", "free", "grep", "head", "help", "hostname", "id", "journalctl",
    "kill", "ls", "lsof", "mkdir", "mv", "netstat", "nproc", "ping",
    "ps", "pwd", "quit", "rm", "sed", "sessions", "ss", "stat", "status",
    "switch", "systemctl", "tail", "top", "uname", "uptime", "vmstat",
    "who", "whoami",
]


class InteractiveRepl:
    """
    Interactive shell attached to a named session.

    Built-in meta-commands (not sent to the remote host):

    =========   ============================================================
    Command     Action
    =========   ============================================================
    help        Print this list
    status      Show connection state and uptime
    sessions    List all registered sessions
    switch <id> Change active session
    disconnect  Disconnect current session and exit the REPL
    exit/quit   Exit the REPL (session stays connected)
    =========   ============================================================
    """

    def __init__(self, manager: "SessionManager") -> None:
        self._manager = manager

    def run(self, session_id: str) -> None:
        session = self._manager.get(session_id)
        if session is None:
            err_console.print(f"[red]Unknown session: '{session_id}'[/red]")
            return

        prompt_sess: PromptSession = PromptSession(
            history=InMemoryHistory(),
            completer=WordCompleter(_COMMON_COMMANDS, ignore_case=True),
            complete_while_typing=False,
        )

        console.print(
            f"\n[green]Connected to[/green] [bold]{session.label}[/bold]"
            f"  [dim](type [cyan]help[/cyan] for commands, [cyan]exit[/cyan] to quit)[/dim]\n"
        )

        while True:
            try:
                raw: str = prompt_sess.prompt(
                    HTML(f"<ansicyan>session:{session_id}</ansicyan> <ansigreen>❯</ansigreen> ")
                )
            except KeyboardInterrupt:
                # Ctrl-C cancels current line, like a real shell
                continue
            except EOFError:
                # Ctrl-D exits
                break

            cmd = raw.strip()
            if not cmd:
                continue

            # ── Meta-commands ───────────────────────────────────────────────
            if cmd in ("exit", "quit"):
                break

            if cmd == "help":
                _print_help()
                continue

            if cmd == "status":
                from workflow_runner.cli.formatter import print_session_table
                print_session_table(self._manager.sessions, active_id=session_id)
                continue

            if cmd == "sessions":
                from workflow_runner.cli.formatter import print_session_table
                print_session_table(self._manager.sessions, active_id=session_id)
                continue

            if cmd == "disconnect":
                session.disconnect()
                console.print("[yellow]Disconnected.[/yellow]")
                break

            if cmd.startswith("switch "):
                new_id = cmd.split(None, 1)[1].strip()
                new_sess = self._manager.get(new_id)
                if new_sess is None:
                    err_console.print(f"[red]Unknown session: '{new_id}'[/red]")
                else:
                    session = new_sess
                    session_id = new_id
                    console.print(
                        f"[green]Switched to[/green] [bold]{session.label}[/bold]"
                    )
                continue

            # ── Destructive-command guard ────────────────────────────────────
            if is_destructive(cmd):
                try:
                    ans: str = prompt_sess.prompt(
                        HTML(
                            "<ansired>⚠ Destructive command detected."
                            " Confirm? [y/N]: </ansired>"
                        )
                    ).strip().lower()
                except (KeyboardInterrupt, EOFError):
                    console.print("[yellow]Aborted.[/yellow]")
                    continue
                if ans != "y":
                    console.print("[yellow]Aborted.[/yellow]")
                    continue

            # ── Remote execution ─────────────────────────────────────────────
            if not session.is_connected():
                console.print("[yellow]Connection lost — reconnecting…[/yellow]")
                if not session.reconnect():
                    err_console.print("[red]Reconnect failed. Exiting.[/red]")
                    break

            def _on_stdout(chunk: str) -> None:
                console.print(chunk, end="")

            def _on_stderr(chunk: str) -> None:
                err_console.print(chunk, end="", style="red")

            try:
                result = session.execute(cmd, on_stdout=_on_stdout, on_stderr=_on_stderr)
                from workflow_runner.cli.formatter import print_command_result
                print_command_result(result)
            except Exception as exc:
                err_console.print(f"[red]Error: {exc}[/red]")


def _print_help() -> None:
    console.print(
        "\n[bold]Interactive Mode[/bold]\n"
        "  [cyan]<linux command>[/cyan]     Execute on remote host (output streamed live)\n"
        "  [cyan]status[/cyan]              Show session info\n"
        "  [cyan]sessions[/cyan]            List all sessions\n"
        "  [cyan]switch <id>[/cyan]         Switch active session\n"
        "  [cyan]disconnect[/cyan]          Disconnect current session and exit\n"
        "  [cyan]exit[/cyan] / [cyan]quit[/cyan]           Exit interactive mode (session stays connected)\n"
        "  [cyan]help[/cyan]                Show this help\n"
    )
