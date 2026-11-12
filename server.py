# Copyright (C) 2026 Aleksandr Motsjonov
# SPDX-License-Identifier: GPL-3.0-or-later
"""Local live dashboard for the upstream UT383BT client."""
import argparse
import asyncio
import csv
import os
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
import math
from pathlib import Path

from aiohttp import WSMsgType, web
from ut383bt.client import UT383BTClient

ROOT = Path(__file__).resolve().parent
HISTORY_SECONDS = 60 * 60
RECORDINGS_DIR = ROOT / "recordings"


class MeterState:
    def __init__(self, interval=1.0):
        self.interval = interval
        self.client = None
        self.readings = deque()
        self.status = "connecting"
        self.detail = "Connecting to the meter…"
        self.last_monotonic = None
        self.segment = 0
        self.listeners = set()
        self.recording = None
        self.recording_info = {"active": False, "filename": None, "message": ""}

    def measurement(self, measurement):
        self.last_monotonic = asyncio.get_running_loop().time()
        if self.status != "connected":
            self.set_status("connected", "Receiving readings")
        self.readings.append({"time": measurement.captured_at.isoformat(), "lux": measurement.lux, "segment": self.segment})
        self.prune()
        if self.recording is not None:
            stream, writer, part, final = self.recording
            try:
                writer.writerow([measurement.captured_at.isoformat(), measurement.lux])
                stream.flush()
            except OSError as exc:
                with suppress(OSError):
                    stream.close()
                self.recording = None
                self.recording_info = {"active": False, "filename": part.name, "message": f"Recording failed: {exc}. Partial data: {part.name}"}
                self.publish({"type": "recording", **self.recording_info})
        self.publish({"type": "measurement", "reading": self.readings[-1]})

    def start_recording(self):
        if self.recording is not None:
            return
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f_UTC")
        final = RECORDINGS_DIR / f"light_{stamp}.csv"
        part = final.with_suffix(".csv.part")
        stream = part.open("x", encoding="utf-8", newline="")
        try:
            writer = csv.writer(stream)
            writer.writerow(["timestamp_utc", "lux"])
            stream.flush()
        except Exception:
            stream.close()
            raise
        self.recording = (stream, writer, part, final)
        self.recording_info = {"active": True, "filename": final.name, "message": "Recording…"}
        self.publish({"type": "recording", **self.recording_info})

    def stop_recording(self):
        if self.recording is None:
            return
        stream, _, part, final = self.recording
        self.recording = None
        try:
            stream.close()
            os.link(part, final)  # Fail instead of overwriting an existing recording.
            part.unlink()
        except OSError as exc:
            self.recording_info = {"active": False, "filename": part.name, "message": f"Could not finish recording: {exc}. Partial data: {part.name}"}
        else:
            self.recording_info = {"active": False, "filename": final.name, "message": f"Saved recordings/{final.name}"}
        self.publish({"type": "recording", **self.recording_info})

    def publish(self, message):
        for ws, queue in tuple(self.listeners):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A slow browser reconnects and receives a fresh snapshot.
                asyncio.create_task(ws.close(code=1013, message=b"Client too slow"))

    def set_status(self, status, detail):
        self.status, self.detail = status, detail
        self.publish({"type": "status", "status": status, "detail": detail})

    def prune(self):
        cutoff = datetime.now(timezone.utc).timestamp() - HISTORY_SECONDS
        while self.readings and datetime.fromisoformat(self.readings[0]["time"]).timestamp() < cutoff:
            self.readings.popleft()

    def snapshot(self):
        self.prune()
        return {"status": self.status, "detail": self.detail, "interval": self.interval, "recording": self.recording_info.copy(), "readings": list(self.readings)}


async def monitor_meter(state, device, interval):
    """Reconnect after an error or a prolonged lack of notifications."""
    loop = asyncio.get_running_loop()
    while True:
        state.set_status("connecting", "Connecting to the meter…")
        state.last_monotonic = None
        state.segment += 1
        started = loop.time()
        client = UT383BTClient(device, command_interval=state.interval)
        state.client = client
        task = asyncio.create_task(client.collect(365 * 24 * 60 * 60, state.measurement))
        try:
            while not task.done():
                await asyncio.sleep(1)
                age = loop.time() - (state.last_monotonic or started)
                limit = max(35, state.interval * 5) if state.last_monotonic is None else max(10, state.interval * 5)
                if age > limit:
                    raise TimeoutError("No readings received from the meter")
            await task
            raise ConnectionError("Meter session ended")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.set_status("disconnected", f"{exc}. Retrying…")
            print(state.detail, flush=True)
        finally:
            state.client = None
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        await asyncio.sleep(5)


async def index(request):
    return web.FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-store"})


async def websocket(request):
    origin = request.headers.get("Origin")
    if origin and origin != f"{request.scheme}://{request.host}":
        raise web.HTTPForbidden(text="Origin does not match this server")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096)
    await ws.prepare(request)
    state = request.app["state"]
    queue = asyncio.Queue(maxsize=100)
    listener = (ws, queue)
    state.listeners.add(listener)
    queue.put_nowait({"type": "snapshot", **state.snapshot()})

    async def writer():
        try:
            while True:
                await ws.send_json(await queue.get())
        except (ConnectionError, RuntimeError):
            await ws.close()

    task = asyncio.create_task(writer())
    try:
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                data = message.json()
                if not isinstance(data, dict):
                    raise ValueError
                if data.get("type") == "start_recording":
                    try:
                        state.start_recording()
                    except OSError as exc:
                        state.recording_info = {"active": False, "filename": None, "message": f"Could not start recording: {exc}"}
                        state.publish({"type": "recording", **state.recording_info})
                    continue
                if data.get("type") == "stop_recording":
                    state.stop_recording()
                    continue
                if data.get("type") == "reset":
                    state.readings.clear()
                    state.publish({"type": "reset"})
                    continue
                value = data["interval"]
                if data.get("type") != "set_interval" or isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                queue.put_nowait({"type": "error", "detail": "Interval must be a finite positive number"})
                continue
            state.interval = float(value)
            if state.client is not None:
                state.client.command_interval = state.interval
            state.publish({"type": "interval", "interval": state.interval})
    finally:
        state.listeners.discard(listener)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    return ws


def create_app(device, interval):
    app = web.Application()
    app["state"] = MeterState(interval)
    app.router.add_get("/", index)
    app.router.add_get("/ws", websocket)

    async def close_websockets(app):
        app["state"].stop_recording()
        await asyncio.gather(*(ws.close(code=1001, message=b"Server shutting down") for ws, _ in tuple(app["state"].listeners)))

    app.on_shutdown.append(close_websockets)

    async def meter_lifecycle(app):
        task = asyncio.create_task(monitor_meter(app["state"], device, interval))
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app.cleanup_ctx.append(meter_lifecycle)
    return app


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, help="Meter identifier from ut383bt scan")
    parser.add_argument("--interval", type=positive, default=1.0, help="Seconds between requests (default: 1)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    print(f"Dashboard: http://127.0.0.1:{args.port}", flush=True)
    web.run_app(create_app(args.device, args.interval), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
