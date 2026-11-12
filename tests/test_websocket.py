# Copyright (C) 2026 Aleksandr Motsjonov
# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import csv
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from ut383bt.measurement import LuxMeasurement

import server


class FakeMeter:
    def __init__(self, device, command_interval):
        self.command_interval = command_interval

    async def collect(self, duration, callback):
        self.callback = callback
        await asyncio.Event().wait()


class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patch = patch.object(server, "UT383BTClient", FakeMeter)
        self.patch.start()
        self.app = server.create_app("test", 1)
        self.http = TestClient(TestServer(self.app))
        await self.http.start_server()
        await asyncio.sleep(0)

    async def asyncTearDown(self):
        await self.http.close()
        self.patch.stop()

    async def test_push_interval_and_reconnect_snapshot(self):
        first = await self.http.ws_connect('/ws')
        second = await self.http.ws_connect('/ws')
        self.assertEqual((await first.receive_json())['type'], 'snapshot')
        await second.receive_json()
        self.app['state'].measurement(LuxMeasurement(123, datetime.now(timezone.utc), b''))
        for ws in (first, second):
            self.assertEqual((await ws.receive_json())['type'], 'status')
            self.assertEqual((await ws.receive_json())['reading']['lux'], 123)
        await first.send_json({'type': 'set_interval', 'interval': 0.5})
        for ws in (first, second):
            self.assertEqual((await ws.receive_json())['interval'], 0.5)
        self.assertEqual(self.app['state'].client.command_interval, 0.5)
        await first.send_json({'type': 'set_interval', 'interval': -1})
        self.assertEqual((await first.receive_json())['type'], 'error')
        await first.close()
        again = await self.http.ws_connect('/ws')
        snapshot = await again.receive_json()
        self.assertEqual(snapshot['readings'][0]['lux'], 123)
        self.assertEqual(snapshot['interval'], 0.5)
        await again.close()
        await second.close()

    async def test_reset_clears_shared_history_and_collection_continues(self):
        first = await self.http.ws_connect('/ws')
        second = await self.http.ws_connect('/ws')
        await first.receive_json()
        await second.receive_json()
        state = self.app['state']
        state.measurement(LuxMeasurement(123, datetime.now(timezone.utc), b''))
        for ws in (first, second):
            await ws.receive_json()  # connected status
            await ws.receive_json()  # measurement
        await first.send_json({'type': 'reset'})
        self.assertEqual((await first.receive_json())['type'], 'reset')
        self.assertEqual((await second.receive_json())['type'], 'reset')
        self.assertEqual(state.snapshot()['readings'], [])
        state.measurement(LuxMeasurement(456, datetime.now(timezone.utc), b''))
        self.assertEqual((await first.receive_json())['reading']['lux'], 456)
        self.assertEqual((await second.receive_json())['reading']['lux'], 456)
        await first.close()
        await second.close()

    async def test_recording_spans_reset_and_saves_only_capture_period(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'RECORDINGS_DIR', Path(directory)):
            first = await self.http.ws_connect('/ws')
            second = await self.http.ws_connect('/ws')
            await first.receive_json()
            await second.receive_json()
            state = self.app['state']
            state.measurement(LuxMeasurement(10, datetime.now(timezone.utc), b''))
            for ws in (first, second):
                await ws.receive_json()
                await ws.receive_json()
            await first.send_json({'type': 'start_recording'})
            started = await first.receive_json()
            self.assertTrue(started['active'])
            self.assertTrue((await second.receive_json())['active'])
            self.assertFalse((Path(directory) / started['filename']).exists())
            state.measurement(LuxMeasurement(20, datetime.now(timezone.utc), b''))
            for ws in (first, second): await ws.receive_json()
            await second.send_json({'type': 'reset'})
            for ws in (first, second): await ws.receive_json()
            state.measurement(LuxMeasurement(30, datetime.now(timezone.utc), b''))
            for ws in (first, second): await ws.receive_json()
            await second.send_json({'type': 'stop_recording'})
            stopped = await first.receive_json()
            self.assertFalse(stopped['active'])
            self.assertFalse((await second.receive_json())['active'])
            path = Path(directory) / started['filename']
            with path.open(newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([row['lux'] for row in rows], ['20', '30'])
            self.assertIn('_UTC.csv', path.name)
            self.assertEqual(list(Path(directory).glob('*.part')), [])
            self.assertFalse(state.snapshot()['recording']['active'])
            await first.close()
            await second.close()

    async def test_rejects_cross_origin(self):
        response = await self.http.get('/ws', headers={'Origin': 'https://example.org'})
        self.assertEqual(response.status, 403)
