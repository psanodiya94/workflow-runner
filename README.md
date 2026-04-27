# workflow-runner

A persistent, interactive remote command execution and workflow runner for
Linux machines. It connects over SSH, stays connected, and lets you execute
ad-hoc commands or predefined workflows — including a `gdb`-style step-by-step
debugger.

```
tool> connect prod1 --host db01.internal --user ops -i ~/.ssh/id_ed25519
session(prod1)> !uptime
session(prod1)> workflow workflows/example.yaml
session(prod1)> debug workflows/deploy.json
```

---

## Highlights

- **Persistent SSH sessions** with keepalive, reconnect-on-drop, and per-session
  state visible at a glance (`status`).
- **Three execution modes:**
  - *Interactive* — type commands, see output streamed live.
  - *Workflow* — run an ordered list of steps from YAML/JSON.
  - *Step-by-step debugger* — `next` / `continue` / `prev` / `stop`, just like
    `gdb`.
- **Security guard** — refuses obviously destructive commands (`rm -rf /`,
  `mkfs`, fork bombs, …) and prompts for confirmation on dangerous ones
  (`rm -rf /tmp/x`, `shutdown`, `iptables -F`, …).
- **Structured, redacted logging** — JSON-line logs to file, plain text to
  stderr, secrets scrubbed everywhere.
- **Multi-session** — open several hosts at once, switch with `use <name>`.
- **Modular and extensible** — swap the SSH transport for anything that
  implements the `Connection` interface; add custom workflow steps; register
  extra security rules.

---

## Architecture

The codebase is organized as four cooperating layers, each in its own package:

```
+---------------------------------------------------------------+
|                        cli/  (REPL, app)                      |
|       prompt_toolkit, Rich, argparse subcommands              |
+----------------+--------------------------+-------------------+
                 |                          |
                 v                          v
+----------------+----------+   +-----------+--------------+
|     workflow/             |   |     debugger/            |
|  - model (Workflow/Step)  |   |  - WorkflowDebugger      |
|  - loader (YAML/JSON)     |<--+    drives the engine     |
|  - engine (iter_steps)    |   |    one step at a time    |
+----------------+----------+   +--------------------------+
                 |
                 v
+----------------+--------------------+
|     execution/                      |
|  - CommandExecutor                  |
|  - CommandResult / ExecutionStatus  |
+----------------+--------------------+
                 |
                 v
+----------------+--------------------+   +---------------------+
|     connection/                     |   |     security/       |
|  - Connection (abstract)            |   |  - SecurityGuard    |
|  - SSHConnection (paramiko)         |   |  - SecurityVerdict  |
|  - LocalConnection (subprocess)     |   |  - validate_command |
|  - SessionManager (multi-session,   |   +---------------------+
|    reconnect, status)               |
+-------------------------------------+

  cross-cutting: logging_utils (structured + redacted)
```

### Component responsibilities

| Package          | Responsibility                                                                 |
|------------------|--------------------------------------------------------------------------------|
| `connection`     | Open / close / keep-alive SSH sessions; pluggable `Connection` interface.      |
| `execution`      | Run a single command, stream output, enforce timeout and security policy.      |
| `workflow`       | Define / load / execute ordered command lists with per-step metadata.          |
| `debugger`       | Drive a workflow iteratively (`next`, `continue`, `prev`, `stop`).             |
| `security`       | Pattern-match destructive commands; raise/return verdicts; validate input.     |
| `cli`            | REPL + one-shot subcommands; rich rendering; user prompts.                     |
| `logging_utils`  | One-call setup of structured logs with sensitive-key redaction.                |

The dependencies point in one direction (CLI → workflow → execution → connection),
so any layer can be lifted out and reused: e.g. drive the engine programmatically
from a Web UI, or supply your own `Connection` for a non-SSH transport.

---

## Installation

Requires **Python 3.9+**. Works on Linux and macOS.

```bash
# clone and install in a virtualenv
git clone https://github.com/psanodiya94/workflow-runner.git
cd workflow-runner
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

The install exposes two equivalent CLI commands: `workflow-runner` and `wfr`.

---

## Usage

### Interactive REPL

```bash
workflow-runner                                # bare REPL, no session
workflow-runner --host db01 --user ops -i ~/.ssh/id_ed25519
```

Inside the REPL:

```
tool> connect prod1 --host db01 --user ops --identity ~/.ssh/id_ed25519
[green]connected[/green] prod1 -> ops@db01:22

session(prod1)> status
session(prod1)> !uptime                      # ! = run on the active session
session(prod1)> run df -hT
session(prod1)> workflow workflows/example.yaml
session(prod1)> debug workflows/deploy.json
session(prod1)> disconnect prod1
tool> exit
```

`help` prints every command with options; `Ctrl-D` exits.

### One-shot subcommands

| Subcommand                       | Purpose                                |
|----------------------------------|----------------------------------------|
| `workflow-runner run --host h --user u -- <cmd>`     | Run one command and exit.        |
| `workflow-runner workflow <file> --host …`           | Run a workflow end-to-end.       |
| `workflow-runner debug    <file> --host …`           | Step through a workflow.         |
| `workflow-runner --local …`                          | Use a local subprocess (dev/CI). |

Connection flags (shared across subcommands):

```
--host HOST           required for SSH
--user USER           required for SSH
--port PORT           default 22
-i, --identity KEY    SSH private key
--password            prompt for SSH password
--no-agent            disable ssh-agent
--insecure            disable strict host-key checking (dev only)
--local               run against /bin/bash on this machine
--timeout SECONDS     per-command timeout
--name NAME           session name (default: "default")
```

### Logging

```
--log-level DEBUG|INFO|WARNING|ERROR
--log-file  PATH      JSONL log; rotates at 5 MB, keeps 3 files
--log-json            emit JSONL on the console as well
```

Secrets (`password`, `token`, `private_key`, `passphrase`, …) are redacted
everywhere — both in messages and structured `extra` fields.

---

## Workflow file format

Workflows are YAML or JSON. Format is inferred from the file extension; unknown
keys are rejected so typos surface immediately.

```yaml
name: system-health-check                # required
description: Quick smoke check for a Linux host.
version: "1"
default_timeout: 30
default_env:
  LANG: C.UTF-8

steps:
  - name: uptime
    command: uptime

  - name: disk-usage
    command: df -hT -x tmpfs -x devtmpfs

  - name: kernel-log-tail
    command: "dmesg | tail -n 20 || echo '(dmesg unavailable)'"
    on_failure: continue            # stop | continue | prompt
    expect_exit_codes: [0, 1]

  - name: cleanup-temp
    command: rm -rf /tmp/workflow-runner-scratch
    confirm: true                   # forces interactive confirmation
    on_failure: continue
```

Per-step keys:

| key                | type         | meaning                                                       |
|--------------------|--------------|---------------------------------------------------------------|
| `name`             | string       | Stable identifier, must be unique within the workflow.        |
| `command`          | string       | Shell command to run on the remote.                           |
| `description`      | string       | Free-form documentation for humans.                           |
| `cwd`              | string       | Working directory on the remote.                              |
| `env`              | mapping      | Extra environment variables for this step.                    |
| `timeout`          | float (sec)  | Override the workflow default timeout.                        |
| `on_failure`       | enum         | `stop` (default), `continue`, or `prompt` (interactive only). |
| `confirm`          | bool         | Force a "type 'yes'" confirmation regardless of severity.     |
| `skip`             | bool         | Skip without executing (handy for staged rollouts).           |
| `tags`             | list[string] | Free-form tags (CLI may filter on these in the future).       |
| `expect_exit_codes`| list[int]    | Treat any of these as success (default `[0]`).                |

### Example CLI session

```
$ workflow-runner workflow workflows/example.yaml --host db01 --user ops
2026-04-27T10:00:00 [INFO   ] workflow_runner.engine :: executing step
$ uptime
 10:00:00 up  3:42, 1 user, load average: 0.12, 0.10, 0.09
exit=0  status=success  duration=0.121s

$ df -hT -x tmpfs -x devtmpfs
Filesystem     Type      Size  Used Avail Use% Mounted on
/dev/sda1      ext4       40G   18G   20G  47% /
exit=0  status=success  duration=0.087s
…
DESTRUCTIVE COMMAND: rm -rf /tmp/workflow-runner-scratch
  reasons: recursive forced delete (`rm -rf`)
type 'yes' to proceed: no
…
       Workflow report: system-health-check
┏━━━━━━━━━━━━━┳━━━━━━━━┓
┃ Metric      ┃  Value ┃
┡━━━━━━━━━━━━━╇━━━━━━━━┩
│ Total steps │      6 │
│ Succeeded   │      5 │
│ Aborted     │      1 │
│ Overall     │ FAILED │
└─────────────┴────────┘
```

### Step-by-step debugger

```
$ workflow-runner debug workflows/deploy.json --host db01 --user ops

debug: deploy-app  (5 steps).
> [step 1/5] git-pull: git pull --ff-only origin main
workflow(debug)> next
$ git pull --ff-only origin main
Already up to date.
exit=0  status=success  duration=0.512s

> [step 2/5] install-deps: ./scripts/install.sh
workflow(debug)> prev      # re-show last step's output
workflow(debug)> continue  # run all remaining steps
workflow(debug)> stop      # abort
```

`prev` is read-only by design — we cannot safely undo a remote command, so
"previous" only re-displays the most recent output.

---

## Security model

`workflow-runner` errs on the side of *not* running things.

1. **Authentication** — SSH keys (with passphrase support), ssh-agent, or
   password (prompt only — never stored, never logged). Strict host-key
   checking is on by default; `--insecure` is the only way off and prints a
   warning every time.
2. **Input validation** — every command is `shlex`-tokenized and checked for
   NUL bytes before being dispatched.
3. **Destructive command guard** — a curated rule set classifies commands as
   `safe`, `caution`, `dangerous`, or `blocked`. `blocked` commands never
   reach the wire; `dangerous` and `caution` trigger an interactive
   confirmation. See `src/workflow_runner/security/guard.py` for the full list.
4. **Secret redaction** — anything matching a known sensitive key
   (`password`, `passphrase`, `token`, `secret`, `api_key`, …) is replaced
   with `***REDACTED***` in both messages and structured fields.

Adding rules:

```python
from workflow_runner.security import SecurityGuard
from workflow_runner.security.guard import Severity

guard = SecurityGuard(extra_rules=[
    ("no-cat-shadow", Severity.DANGEROUS, r"\bcat\b.*\b/etc/shadow\b", "exposes /etc/shadow"),
])
```

---

## Programmatic API

```python
from workflow_runner.connection import ConnectionConfig, SessionManager
from workflow_runner.execution import CommandExecutor
from workflow_runner.workflow import WorkflowEngine, load_workflow

sessions = SessionManager()
sessions.add(ConnectionConfig(name="db01", host="db01", username="ops",
                              key_filename="~/.ssh/id_ed25519"))
executor = CommandExecutor(sessions.get("db01"))
report = WorkflowEngine(load_workflow("workflows/deploy.json"), executor).run_all()
print(report.to_dict())
```

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest
ruff check src tests
```

The `LocalConnection` transport lets the test suite (and your local
experimentation) run end-to-end without any SSH host:

```bash
workflow-runner workflow workflows/example.yaml --local
```

---

## Extensibility

The current code already accommodates these extensions without invasive
changes:

- **File upload/download** — add a method to the `Connection` ABC and
  implement it via paramiko's `SFTPClient`.
- **Environment variable injection** — already exposed as `Step.env` /
  `default_env`; just add CLI flags.
- **Parallel execution across hosts** — `SessionManager` is thread-safe;
  wrap `WorkflowEngine.run_all()` calls in a `ThreadPoolExecutor`.
- **Plugin system for custom workflows** — load steps from
  `entry_points = "workflow_runner.steps"` and register them with the
  engine.

---

## License

MIT. See `pyproject.toml`.
