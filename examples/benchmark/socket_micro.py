"""Socket round-trip cost per event loop and allocator.

Backs the uvloop section of RESULTS.md. The matrix cannot answer this on its
own: its I/O is asyncio.sleep(), a timer, while uvloop replaces the transport.

Start fakeapi.py on cores the client does not use, then:

    taskset -c 28,29 .venv-async-314/bin/python socket_micro.py stdlib
    PYTHONMALLOC=mimalloc taskset -c 28,29 .venv-async-314/bin/python socket_micro.py stdlib
    taskset -c 28,29 .venv-async-314/bin/python socket_micro.py uvloop
"""

import asyncio
import resource
import sys
import time

TOTAL = 8000
CONC = 16
PORT = 8999


async def client(n: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    for _ in range(n):
        writer.write(b"256\n")
        await writer.drain()
        await reader.readline()
    writer.close()
    await writer.wait_closed()


async def bench() -> None:
    await asyncio.gather(*[client(TOTAL // CONC) for _ in range(CONC)])


def main() -> None:
    impl = sys.argv[1] if len(sys.argv) > 1 else "stdlib"
    if impl == "uvloop":
        import uvloop

        loop = uvloop.new_event_loop()
    else:
        loop = asyncio.new_event_loop()

    loop.run_until_complete(bench())
    r0 = resource.getrusage(resource.RUSAGE_SELF)
    t0 = time.monotonic()
    loop.run_until_complete(bench())
    dt = time.monotonic() - t0
    r1 = resource.getrusage(resource.RUSAGE_SELF)
    loop.close()

    cpu = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
    print(f"{impl:8} {TOTAL / dt:8.0f} round-trips/s   {cpu / TOTAL * 1e6:6.1f} us CPU each")


if __name__ == "__main__":
    main()
