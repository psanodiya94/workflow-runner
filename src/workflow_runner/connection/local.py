"""Local subprocess transport.

Used for tests and for ``--target local`` smoke runs without a remote host.
Does *not* attempt feature-parity with SSH (no env injection beyond
``subprocess`` semantics, no PTY) — just enough to drive the executor
end-to-end.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from workflow_runner.connection.base import (
    Connection,
    ConnectionError,
    ConnectionState,
    OnChunk,
)


class LocalConnection(Connection):
    """Run commands on the local machine via ``/bin/bash -c``."""

    def __init__(self, *, shell: str = "/bin/bash") -> None:
        self._shell = shell
        self._state = ConnectionState.DISCONNECTED

    @property
    def state(self) -> ConnectionState:
        return self._state

    def describe(self) -> str:
        return f"local://{os.uname().nodename}"

    def connect(self) -> None:
        if not os.path.exists(self._shell):
            raise ConnectionError(f"shell not found: {self._shell}")
        self._state = ConnectionState.CONNECTED

    def close(self) -> None:
        self._state = ConnectionState.CLOSED

    def is_alive(self) -> bool:
        return self._state is ConnectionState.CONNECTED

    def exec_command(
        self,
        command: str,
        *,
        on_chunk: OnChunk,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        deadline: float | None = None,
    ) -> int:
        if not self.is_alive():
            raise ConnectionError("local connection not initialised; call connect()")

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        proc = subprocess.Popen(
            [self._shell, "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=merged_env,
            text=True,
            bufsize=1,
        )

        def _pump(stream, name: str) -> None:
            for line in iter(stream.readline, ""):
                on_chunk(name, line)
            stream.close()

        threads = [
            threading.Thread(target=_pump, args=(proc.stdout, "stdout"), daemon=True),
            threading.Thread(target=_pump, args=(proc.stderr, "stderr"), daemon=True),
        ]
        for t in threads:
            t.start()

        while True:
            if deadline is not None and time.time() > deadline:
                proc.kill()
                for t in threads:
                    t.join(timeout=1.0)
                raise TimeoutError("command timed out")
            try:
                return proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                continue
            finally:
                if proc.poll() is not None:
                    for t in threads:
                        t.join(timeout=1.0)
