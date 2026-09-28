"""CLI entry point."""

import asyncio

from .app import run_bot
from .config import Settings


def main() -> None:
    asyncio.run(run_bot(Settings()))


if __name__ == "__main__":
    main()
