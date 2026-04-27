"""Package entry point — delegates directly to the Click CLI."""

from workflow_runner.cli.interface import cli

if __name__ == "__main__":
    cli()
