import asyncio
import selectors
import sys


def loop_factory():
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()


def run(coroutine):
    return asyncio.run(coroutine, loop_factory=loop_factory)
