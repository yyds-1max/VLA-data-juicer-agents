"""Local, production-faithful evaluation support for VLA agents."""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Run the evaluation command line interface."""

    from .cli import main as cli_main

    return cli_main(argv)
