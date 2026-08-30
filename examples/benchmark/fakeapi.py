"""Minimal TCP service standing in for an HTTP API the tasks call.

Deliberately not aiohttp/httpx on either side: a real HTTP stack spends more
time parsing than the event loop spends on the socket, which is exactly what
masks the difference this benchmark exists to measure. A two-field line
protocol keeps the measurement on the transport.

Always runs on the stdlib selector loop so it stays a constant while the
worker's loop is the variable. Pin it to cores the worker does not use, or
its CPU lands in the worker's numbers.

    python fakeapi.py --port 8971 --delay 0.1
"""

import argparse
import asyncio


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, delay: float) -> None:
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            size = int(line.strip())
        except ValueError:
            break
        if delay > 0:
            await asyncio.sleep(delay)
        writer.write(b"X" * size + b"\n")
        await writer.drain()
    writer.close()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8971)
    ap.add_argument("--delay", type=float, default=0.0, help="server-side think time per request")
    args = ap.parse_args()

    server = await asyncio.start_server(lambda r, w: handle(r, w, args.delay), args.host, args.port, backlog=2048)
    print(f"[fakeapi] listening on {args.host}:{args.port} delay={args.delay}s", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
