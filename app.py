"""Hold a simulated iPhone location over Wi-Fi, controlled by a small PWA.

One process: FastAPI serves the PWA + API; a worker task keeps a userspace tunnel to the phone
(no root, no /dev/net/tun) and re-sends the location. The worker turns itself off after
IDLE_TIMEOUT_MINUTES without a connection.
"""
import asyncio
import json
import logging
import os
import plistlib
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import requests
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pymobiledevice3.exceptions import AlreadyMountedError
from pymobiledevice3.lockdown import create_using_tcp
from pymobiledevice3.remote import userspace_tunnel
from pymobiledevice3.remote.tunnel_service import CoreDeviceTunnelProxy
from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.services.heartbeat import HeartbeatService
from pymobiledevice3.services.mobile_image_mounter import auto_mount

PHONE_IP = os.environ["PHONE_IP"]
PAIR_RECORD = plistlib.loads(Path(os.environ.get("PAIR_RECORD", "/pair_record.plist")).read_bytes())
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
IDLE_TIMEOUT = float(os.environ.get("IDLE_TIMEOUT_MINUTES", "60")) * 60
HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_NOTIFY = os.environ.get("HA_NOTIFY", "")  # e.g. mobile_app_my_iphone
REASSERT_SECONDS = 5  # iOS drops the simulation when a stale session times out, so re-send it
RETRY_SECONDS = 10
SETUP_TIMEOUT = 120  # heartbeat + tunnel + disk image mount (a first-time image download takes a while)
LIVE_ACTIVITY_REFRESH = 7 * 3600  # iOS ends Live Activities after 8h
LIVE_ACTIVITY_TAG = "ios-location"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ios-location")


async def _wifi_tunnel_provider(serial, autopair, remotepairing_fallback=True):
    lockdown = await create_using_tcp(PHONE_IP, pair_record=PAIR_RECORD, autopair=False)
    try:
        return await CoreDeviceTunnelProxy.create(lockdown), lockdown
    except BaseException:
        await lockdown.close()
        raise


# ponytail: patches a private hook of pymobiledevice3 11.27.2 (upstream only reaches it via usbmux);
# re-check userspace_tunnel._aopen_locked when bumping the pinned version.
userspace_tunnel._create_no_root_tunnel_provider = _wifi_tunnel_provider


@asynccontextmanager
async def heartbeat_session():
    lockdown = await create_using_tcp(PHONE_IP, pair_record=PAIR_RECORD, autopair=False)
    try:
        service = await asyncio.wait_for(lockdown.start_lockdown_service(HeartbeatService.SERVICE_NAME), 15)
        try:
            yield service
        finally:
            with suppress(Exception):
                await service.close()  # pymobiledevice3's HeartbeatService never closes it
    finally:
        with suppress(Exception):
            await lockdown.close()


@asynccontextmanager
async def heartbeat():
    """iOS only accepts (and keeps) Wi-Fi tunnels while the host answers the heartbeat service.

    The phone sends Marco every ~10s and expects Polo; before deep sleep it sends SleepyTime and
    hangs up, so the loop reconnects instead of giving up.
    """
    ready = asyncio.Event()

    async def keep():
        while True:
            try:
                async with heartbeat_session() as service:
                    ready.set()
                    while (await asyncio.wait_for(service.recv_plist(), 60)).get("Command") != "SleepyTime":
                        await service.send_plist({"Command": "Polo"})
                    log.info("heartbeat: phone is going to sleep, reconnecting")
            except Exception as e:
                log.info("heartbeat lost: %r", e)
            await asyncio.sleep(5)

    task = asyncio.create_task(keep())
    try:
        await asyncio.wait_for(ready.wait(), 30)
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class State:
    def __init__(self):
        saved = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        self.service = saved.get("service", False)
        self.spoof = saved.get("spoof", True)
        self.lat = saved.get("lat", 48.858370)
        self.lon = saved.get("lon", 2.294481)
        self.status = "off"  # off | searching | connected
        self.last_seen = time.time()  # last connection (or service start); drives the idle switch

    def save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({k: getattr(self, k) for k in ("service", "spoof", "lat", "lon")}))


state = State()
changed = asyncio.Event()  # wakes the hold loop when the target or spoof toggle changes
worker_task: asyncio.Task | None = None
notified: tuple | None = None
notified_at = 0.0
notify_task: asyncio.Task | None = None


def _post_ha(body):
    try:
        requests.post(
            f"{HA_URL}/api/services/notify/{HA_NOTIFY}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            json=body,
            timeout=10,
        ).raise_for_status()
    except requests.RequestException as e:
        log.warning("Home Assistant notify failed: %s", e)


def notify(force=False):
    """Mirror status into a Live Activity; only on change, since iOS throttles pushes."""
    global notified, notified_at, notify_task
    key = (state.status, state.spoof if state.status == "connected" else None)
    if not (HA_URL and HA_TOKEN and HA_NOTIFY) or (key == notified and not force):
        return
    notified, notified_at = key, time.time()
    if state.status == "off":
        bodies = [{"message": "clear_notification", "data": {"tag": LIVE_ACTIVITY_TAG}}]
    else:
        text = {"searching": "Searching for iPhone…", "connected": "Connected"}[state.status]
        if state.status == "connected":
            text += f" · fake location {'on' if state.spoof else 'off'}"
        start = {"title": "iPhone location", "message": text,
                 "data": {"tag": LIVE_ACTIVITY_TAG, "live_update": True, "silent": True}}
        # Past the 8h limit: end the old activity and start a fresh one.
        bodies = [{"message": "clear_notification", "data": {"tag": LIVE_ACTIVITY_TAG}}, start] if force else [start]
    previous = notify_task

    async def send():  # chained so pushes reach HA in order
        if previous:
            await previous
        for body in bodies:
            await asyncio.to_thread(_post_ha, body)

    notify_task = asyncio.create_task(send())


def set_status(status):
    if state.status != status:
        log.info("status: %s", status)
    state.status = status
    notify()


async def hold(sim, info):
    cleared = False
    while True:
        state.last_seen = time.time()
        if state.spoof:
            await asyncio.wait_for(sim.set(state.lat, state.lon), timeout=10)
            cleared = False
        else:
            if not cleared:
                await sim.clear()
                cleared = True
            await asyncio.wait_for(info.is_running_pid(1), timeout=10)  # liveness probe
        if time.time() - notified_at > LIVE_ACTIVITY_REFRESH:
            notify(force=True)
        changed.clear()
        with suppress(TimeoutError):
            await asyncio.wait_for(changed.wait(), REASSERT_SECONDS)


async def worker():
    state.last_seen = time.time()
    while True:
        set_status("searching")
        try:
            # A phone that never answers the tunnel handshake would otherwise block this forever.
            async with asyncio.timeout(SETUP_TIMEOUT) as setup:
                async with heartbeat(), userspace_tunnel.UserspaceRsdTunnel() as rsd:
                    with suppress(AlreadyMountedError):
                        await auto_mount(rsd)
                        log.info("developer disk image ready")
                    async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as sim, DeviceInfo(dvt) as info:
                        setup.reschedule(None)
                        set_status("connected")
                        try:
                            await hold(sim, info)
                        finally:
                            with suppress(Exception):
                                await sim.clear()
        except Exception as e:
            log.info("session ended: %r", e)
        if time.time() - state.last_seen > IDLE_TIMEOUT:
            log.info("no connection for %d min, turning off", IDLE_TIMEOUT // 60)
            state.service = False
            state.save()
            set_status("off")
            return
        await asyncio.sleep(RETRY_SECONDS)


async def stop_worker():
    global worker_task
    if worker_task:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task
        worker_task = None
    set_status("off")


def start_worker():
    global worker_task
    if not worker_task or worker_task.done():
        set_status("searching")
        worker_task = asyncio.create_task(worker())


@asynccontextmanager
async def lifespan(_app):
    if state.service:
        start_worker()
    yield
    await stop_worker()
    if notify_task:
        await notify_task  # let the "off" push go out before exit


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class Toggle(BaseModel):
    on: bool


class Location(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


@app.get("/api/state")
async def get_state():
    idle_off_in = None
    if state.service and state.status != "connected":
        idle_off_in = max(0, round(IDLE_TIMEOUT - (time.time() - state.last_seen)))
    return {"service": state.service, "spoof": state.spoof, "lat": state.lat, "lon": state.lon,
            "status": state.status, "idle_off_in": idle_off_in}


@app.post("/api/service")
async def set_service(t: Toggle):
    state.service = t.on
    state.save()
    if t.on:
        start_worker()
    else:
        await stop_worker()
    return await get_state()


@app.post("/api/spoof")
async def set_spoof(t: Toggle):
    state.spoof = t.on
    state.save()
    changed.set()
    notify()
    return await get_state()


@app.post("/api/location")
async def set_location(loc: Location):
    state.lat, state.lon = loc.lat, loc.lon
    state.save()
    changed.set()
    return await get_state()


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
