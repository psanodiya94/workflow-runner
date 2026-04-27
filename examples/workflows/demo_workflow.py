"""
Workflow defined as a Python module.

Any .py workflow file must expose a top-level ``WORKFLOW`` dictionary
with the same schema as the YAML/JSON format.  This is useful when you
need to generate steps programmatically.
"""

import socket

# Build steps dynamically (e.g. based on local hostname)
_extra_note = f"(triggered from {socket.gethostname()})"

WORKFLOW = {
    "name": "python_demo",
    "version": "1.0",
    "description": f"Demo workflow defined as a Python module {_extra_note}",
    "environment": {
        "GREETING": "Hello from wfr",
    },
    "steps": [
        {
            "name": "greet",
            "command": 'echo "$GREETING"',
            "description": "Print the greeting env variable",
        },
        {
            "name": "date_time",
            "command": "date -u '+%Y-%m-%dT%H:%M:%SZ'",
            "description": "Current UTC timestamp on the remote host",
        },
        {
            "name": "kernel_version",
            "command": "uname -r",
            "description": "Remote kernel version",
        },
        {
            "name": "list_home",
            "command": "ls -la ~",
            "description": "List home directory contents",
        },
    ],
}
