"""Allow ``python -m workflow_runner`` to launch the CLI."""

from workflow_runner.cli.app import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
