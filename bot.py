"""Compatibility launcher for the Windows command `py bot.py`."""
import asyncio

from app import main


if __name__ == "__main__":
    asyncio.run(main())
