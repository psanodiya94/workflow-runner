# workflow-runner (`wfr`)

A Python-based remote command execution tool for Linux/macOS that connects to
remote hosts over SSH and provides three execution modes:

| Mode | Description |
|---|---|
| **Interactive shell** | Type commands; output streams in real time |
| **Workflow run** | Execute a predefined set of steps end-to-end |
| **Step debugger** | Walk a workflow one step at a time, gdb-style |

---

## Architecture Overview

```
workflow-runner/
├── workflow_runner/
│   ├── connection/          # SSH transport layer
│   │   ├── session.py       # Persistent session, reconnect, streaming exec
│   │   └── manager.py       # In-process session pool
│   ├── executor/            # Command-level concerns
│   │   ├── result.py        # Immutable CommandResult dataclass
│   │   └── command.py       # Destructive-pattern detection, env helpers
│   ├── workflow/            # Workflow layer
│   │   ├── models.py        # Workflow / Step dataclasses
│   │   ├── loader.py        # YAML / JSON / Python loaders
│   │   └── engine.py        # WorkflowEngine + WorkflowDebugger
│   ├── cli/                 # User interface
│   │   ├── formatter.py     # Rich output (tables, panels, rules)
│   │   ├── repl.py          # prompt_toolkit interactive REPL
│   │   └── interface.py     # Click CLI (shell / run / debug commands)
│   ├── logger.py            # Structured per-session log files
│   └── main.py              # Entry point
├── examples/workflows/      # Example workflow definitions
└── tests/                   # pytest test suite
```

### Component Responsibilities

**`connection/session.py`**
Wraps a `paramiko.SSHClient`. The session state machine
(`DISCONNECTED → CONNECTING → CONNECTED`) is thread-safe via an `RLock`.
`execute()` streams stdout/stderr via `select` and accepts optional
`on_stdout`/`on_stderr` callbacks for live display, returning an immutable
`CommandResult` when the remote process exits. On transport failure the
session auto-reconnects up to `max_reconnect_attempts` times with exponential
back-off.

**`connection/manager.py`**
Maintains a named dict of `Session` objects so that one process can hold
multiple simultaneous connections. The CLI creates a session, uses it, and
removes it within a single command invocation.

**`executor/result.py`**
`CommandResult` is a frozen dataclass: command text, exit code, stdout,
stderr, wall-clock execution time, timestamp, and owning session ID.

**`executor/command.py`**
`is_destructive()` pattern-matches against a curated list of dangerous
commands (`rm`, `dd`, `mkfs`, `shutdown`, ...) and triggers a confirmation
prompt. `build_env_prefix()` injects environment variables as a portable
`env KEY=VALUE ...` shell prefix instead of relying on `sshd AcceptEnv`.
`sanitize_env()` redacts sensitive keys from log output.

**`workflow/models.py`**
`Step` — name, command, optional timeout, `allow_failure`, `confirm_before`,
per-step environment overrides.
`Workflow` — ordered `Step` list with a shared base environment and
`from_dict()` factory for deserialization.

**`workflow/loader.py`**
`load_workflow(path)` dispatches to a YAML, JSON, or Python module parser.
Python modules must expose a top-level `WORKFLOW` dict, enabling
programmatic step generation.

**`workflow/engine.py`**
`WorkflowEngine.run()` executes all steps sequentially with optional
`on_step_start`/`on_step_done`/`on_confirm` hooks. A failed step without
`allow_failure` aborts the run.

`WorkflowDebugger` implements gdb-like controls (`step_next`, `step_continue`,
`step_abort`). It maintains its own cursor and state machine
(`READY → PAUSED → COMPLETED | ABORTED`) and emits per-step output via an
optional `on_step_output` callback.

**`cli/formatter.py`**
All Rich output (session tables, workflow summary tables, step banners,
stdout/stderr panels) lives here, keeping the rest of the code display-free.

**`cli/repl.py`**
`InteractiveRepl` drives a `prompt_toolkit.PromptSession` loop. Meta-commands
(`help`, `status`, `switch`, `disconnect`, `exit`) are handled locally; all
other input is forwarded to the remote host with live streaming output.

**`cli/interface.py`**
Three Click commands share a common `_build_session()` helper (connect →
use → disconnect per invocation):

- `wfr shell HOST` — interactive REPL
- `wfr run WORKFLOW HOST` — full workflow execution
- `wfr debug WORKFLOW HOST` — step-by-step debugger

**`logger.py`**
`get_logger(name, session_id=...)` returns a logger with two handlers:
a stderr console handler (WARNING by default, DEBUG with `--verbose`) and a
file handler that writes to `~/.workflow_runner/logs/`.

---

## Installation

**Requires Python >= 3.10.**

```bash
git clone https://github.com/psanodiya94/workflow-runner.git
cd workflow-runner
pip install -e .
```

Dependencies installed automatically:
- `paramiko` — SSH transport
- `rich` — terminal output
- `click` — CLI framework
- `pyyaml` — YAML parser
- `prompt_toolkit` — interactive REPL with history and completions

---

## Usage

### Interactive shell

```
wfr shell [user@]host[:port] [OPTIONS]

Options:
  -i, --key PATH          SSH private key file
  --password              Prompt for SSH password
  -s, --session-id TEXT   Label for logging
  --timeout FLOAT         Connection timeout in seconds  [default: 30.0]
  -v, --verbose           Enable DEBUG logging
```

```bash
# Key-based auth (default: uses SSH agent / ~/.ssh/id_*)
wfr shell admin@10.0.0.5

# Explicit key
wfr shell deploy@prod.example.com:2222 -i ~/.ssh/deploy_key

# Password auth
wfr shell root@192.168.1.10 --password
```

**Interactive session commands:**

```
session:admin@10.0.0.5 ❯ whoami
admin
exit: 0  (0.12s)

session:admin@10.0.0.5 ❯ status         # show connection info
session:admin@10.0.0.5 ❯ help           # print help
session:admin@10.0.0.5 ❯ exit           # leave REPL
session:admin@10.0.0.5 ❯ disconnect     # disconnect and exit
```

---

### Run a workflow

```
wfr run WORKFLOW_FILE [user@]host[:port] [OPTIONS]
```

```bash
wfr run examples/workflows/system_check.yaml admin@10.0.0.5

wfr run examples/workflows/deploy.yaml deploy@prod.example.com -i ~/.ssh/id_ed25519
```

Output streams live per step, then a summary table is printed:

```
Workflow: system_check  v1.0

──── Step 1/9: hostname ────
$ hostname -f
myserver.example.com
  ✓ exit=0  time=0.15s

...

┌──────────────────────────────────────────────────────┐
│               Workflow: system_check                 │
├────┬──────────────────────┬────────┬──────┬─────────┤
│  # │ Step Name            │ Status │ Exit │    Time │
├────┼──────────────────────┼────────┼──────┼─────────┤
│  1 │ hostname             │ OK     │    0 │  0.15s  │
│  2 │ os_release           │ OK     │    0 │  0.09s  │
│  ... │                   │        │      │         │
└────┴──────────────────────┴────────┴──────┴─────────┘
Status: COMPLETED  elapsed=1.43s  steps=9/9
```

---

### Step-by-step debugger

```
wfr debug WORKFLOW_FILE [user@]host[:port] [OPTIONS]
```

```bash
wfr debug examples/workflows/deploy.yaml deploy@prod.example.com
```

Debugger commands at each step prompt:

| Command | Alias | Action |
|---|---|---|
| `next` | `n` / Enter | Execute current step, pause before next |
| `continue` | `c` | Execute all remaining steps without pausing |
| `stop` | `s` | Abort the workflow immediately |
| `back` | `b` | Show previous step's output again |
| `list` | `l` | List all steps with execution status |
| `help` | `h` | Show help |

```
Debugger: app_deploy  v1.0
9 steps — commands: next|n, continue|c, stop|s, back|b, list|l, help|h

► Step 1/9: check_user
  Confirm the user running the deployment
  Command: whoami && id
debugger ❯ n

  Exit: 0  Time: 0.148s
╭─ stdout ───────────────────────────────────────────╮
│ deploy                                             │
│ uid=1001(deploy) gid=1001(deploy)                  │
╰────────────────────────────────────────────────────╯

► Step 2/9: check_app_dir
  ...
debugger ❯ c    ← run everything from here

Status: COMPLETED  elapsed=12.34s  steps=9/9
```

---

## Workflow Definition Formats

### YAML (recommended)

```yaml
name: my_workflow
version: "1.0"
description: "What this workflow does"

environment:           # shared across all steps
  APP_DIR: /opt/myapp

steps:
  - name: step_name
    command: echo hello
    description: "Optional human-readable description"
    timeout: 30.0          # seconds; omit for no limit
    allow_failure: false   # true = non-zero exit continues the workflow
    confirm_before: false  # true = always prompt before running
    environment:           # per-step overrides
      EXTRA: value
```

### JSON

```json
{
  "name": "health_check",
  "version": "1.0",
  "steps": [
    { "name": "ping", "command": "curl -sf http://localhost/health" }
  ]
}
```

### Python module

```python
# workflow.py — must expose a top-level WORKFLOW dict
WORKFLOW = {
    "name": "dynamic_workflow",
    "steps": [
        {"name": "s1", "command": "echo dynamic"},
    ],
}
```

---

## Security Notes

- **SSH keys preferred** — password auth is supported but keys are strongly
  recommended.
- **Passwords are never logged** — `SessionConfig.password` is stored only in
  memory and never written to any log file.
- **Destructive command guard** — commands matching patterns like `rm`, `dd`,
  `mkfs`, `shutdown`, `kill`, etc. trigger a mandatory confirmation prompt in
  both interactive and workflow modes.
- **Environment sanitization** — keys containing `password`, `token`, `secret`,
  `key`, `credential`, or `auth` are redacted to `***` in log output.
- **No local shell injection** — commands are passed directly to
  `paramiko.SSHClient.exec_command()`, bypassing any local shell.

---

## Logs

Session and workflow logs are written to `~/.workflow_runner/logs/` with
structured timestamps. Pass `--verbose` to any command to also see DEBUG
messages on stderr.

---

## Running Tests

```bash
pip install -e ".[dev]"
pytest
```

---

## Extending the Tool

| Extension point | Where |
|---|---|
| New workflow format | Add a `_load_*` function in `workflow/loader.py` |
| Custom destructive patterns | Add regex to `_DESTRUCTIVE_PATTERNS` in `executor/command.py` |
| Additional CLI commands | Add a `@cli.command` function in `cli/interface.py` |
| File upload/download | Expose `paramiko.SFTPClient` from `connection/session.py` |
| Parallel multi-host execution | Create multiple `Session` objects and use `concurrent.futures.ThreadPoolExecutor` |
| Plugin workflows | Scan a directory for `*.py` / `*.yaml` files at startup |
