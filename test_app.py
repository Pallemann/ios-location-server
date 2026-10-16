"""No-phone check: a tunnel setup that never answers must time out, retry, and trip the idle failswitch.

Run with pymobiledevice3 installed:  python test_app.py
"""
import asyncio
import os
import plistlib
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
(tmp / "pair_record.plist").write_bytes(plistlib.dumps({}))
os.environ.update(PHONE_IP="192.0.2.1", PAIR_RECORD=str(tmp / "pair_record.plist"), STATE_FILE=str(tmp / "state.json"))

import app  # noqa: E402  (needs the env above)


@asynccontextmanager
async def no_heartbeat():
    yield


class HangingTunnel:
    async def __aenter__(self):
        await asyncio.Event().wait()  # a phone that never answers the handshake

    async def __aexit__(self, *exc):
        pass


async def main():
    app.heartbeat = no_heartbeat
    app.userspace_tunnel.UserspaceRsdTunnel = HangingTunnel
    app.SETUP_TIMEOUT, app.RETRY_SECONDS, app.IDLE_TIMEOUT = 1, 0.2, 3

    app.start_worker()
    await asyncio.wait_for(app.worker_task, 15)  # must return on its own via the failswitch
    assert app.state.service is False and app.state.status == "off", (app.state.service, app.state.status)
    assert '"service": false' in (tmp / "state.json").read_text()  # the off-switch survives a restart
    print("PASS")


asyncio.run(main())
