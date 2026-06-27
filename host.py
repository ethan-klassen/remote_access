import asyncio
import fractions
import importlib
import json
import os
import queue
import sys
import threading
import time
import ctypes
from pathlib import Path
from ctypes import wintypes

try:
    import tkinter as tk
except Exception:
    tk = None

try:
    from PIL import Image
except Exception:
    Image = None

pystray = None
try:
    pystray = importlib.import_module("pystray")
except Exception:
    pystray = None

from aiohttp import web
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack
)
from aiortc.contrib.media import MediaPlayer
from aiortc.rtcrtpsender import RTCRtpSender

import av
import mss
import pydirectinput

pydirectinput.PAUSE = 0.0
pydirectinput.FAILSAFE = False

from pynput.keyboard import Controller as KeyController, Key
keyboard = KeyController()

import aiortc.codecs.vpx
aiortc.codecs.vpx.DEFAULT_BITRATE = 6000000

aiortc.codecs.vpx.MAX_BITRATE = 10000000      # 10 Mbps allowance

# 2. Target the modern PyAV VpxEncoder class wrapper
# 'deadline' and 'cpu-used' are passed to FFmpeg/libvpx options dictionary
aiortc.codecs.vpx.Vp8Encoder.options = {
    "deadline": "realtime",
    "cpu-used": "5",        # Values 4-6 trade minimal compression for massive CPU drops
    "tune": "zerolatency"   # Tells the encoder to instantly emit frames without lookahead
}

# Native Windows API Structural Layouts for SendInput
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000  
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800

# Win32 Point structure for tracking the real hardware cursor location
class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p)
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", INPUT_UNION)
    ]

def send_hardware_input(flags, x=0, y=0, data=0):
    extra = ctypes.c_void_p(0)
    mi = MOUSEINPUT(x, y, data, flags, 0, extra)
    u = INPUT_UNION(mi=mi)
    input_struct = INPUT(type=INPUT_MOUSE, u=u)
    ctypes.windll.user32.SendInput(1, ctypes.byref(input_struct), ctypes.sizeof(input_struct))

def native_win32_scroll(clicks):
    wheel_delta = clicks * 120
    send_hardware_input(MOUSEEVENTF_WHEEL, 0, 0, wheel_delta)

def get_windows_cursor_position():
    """Queries the OS directly for where the system cursor currently resides."""
    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y

# System audio capture
player = MediaPlayer(
    "audio=CABLE Output (VB-Audio Virtual Cable)",
    format="dshow",
    options={"audio_buffer_size": "20"}
)

class ScreenCaptureTrack(VideoStreamTrack):
    def __init__(self):
        super().__init__()
        self.sct = mss.MSS()
        raw_monitor = self.sct.monitors[1] 

        self.width = (raw_monitor["width"] // 16) * 16
        self.height = (raw_monitor["height"] // 16) * 16

        self.monitor = {
            "top": raw_monitor["top"],
            "left": raw_monitor["left"],
            "width": self.width,
            "height": self.height
        }
        self.start_time = time.time()
        print(f"Streaming desktop monitor 1 at {self.width}x{self.height}")

    async def recv(self):
        start_loop = time.time()
        elapsed = time.time() - self.start_time
        pts = int(elapsed * 90000)

        # Grab the hardware monitor frame buffer
        img = self.sct.grab(self.monitor)
        
        # Convert raw frame to mutable byte array to inject our hardware cursor
        frame_bytes = bytearray(img.bgra)
        
        try:
            # Query cursor
            mx, my = get_windows_cursor_position()
            rx = mx - self.monitor["left"]
            ry = my - self.monitor["top"]
            
            # Fast bounded box draw without nested loops
            if 3 <= rx < self.width - 3 and 3 <= ry < self.height - 3:
                # Pre-calculate a cyan row slice (7 pixels wide = 28 bytes in BGRA)
                # Cyan is B=255, G=255, R=0, A=255
                cyan_row = bytearray([255, 255, 0, 255] * 7)
                
                # Directly slice rows into the bytearray memory buffer
                for dy in range(-3, 4):
                    row_start = ((ry + dy) * self.width + (rx - 3)) * 4
                    frame_bytes[row_start : row_start + 28] = cyan_row
        except Exception:
            pass

        bgra_frame = av.VideoFrame(self.width, self.height, format="bgra")
        bgra_frame.planes[0].update(frame_bytes)
        
        # Offload the CPU-heavy format translation to an external worker thread
        loop = asyncio.get_event_loop()
        frame = await loop.run_in_executor(
            None, 
            lambda: bgra_frame.reformat(
                width=self.width,
                height=self.height,
                format="yuv420p",
                interpolation="FAST_BILINEAR"
            )
        )

        frame.pts = pts
        frame.time_base = fractions.Fraction(1, 90000)

        process_time = time.time() - start_loop
        sleep_duration = max(0, (1 / 30) - process_time)
        
        # Keep this slight cushion sleep so the encoder thread can breathe
        await asyncio.sleep(sleep_duration if sleep_duration > 0 else 0.001)

        return frame

async def index(request):
    return web.FileResponse(str(BASE_DIR / "index.html"))

active_loops = {}
if getattr(sys, "_MEIPASS", None):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent
CLIENT_METADATA_FILE = BASE_DIR / "client_metadata.json"
GUI_ENABLED = os.environ.get("REMOTE_ACCESS_GUI", "1").lower() not in {"0", "false", "no", "off"}
GUI_QUEUE = queue.Queue()
GUI_STATE = {
    "status": "Idle",
    "address": "http://0.0.0.0:8080",
    "last_client": "None",
    "connections": 0,
    "last_metadata": {},
}
GUI_LOCK = threading.Lock()
GUI_THREAD = None
GUI_ROOT = None
TRAY_ICON = None
SERVER_CONTROLLER = None


class RemoteAccessServerController:
    def __init__(self, app_factory, host="0.0.0.0", port=8080):
        self.app_factory = app_factory
        self.host = host
        self.port = port
        self.running = False
        self._thread = None
        self._loop = None
        self._runner = None
        self._site = None
        self._stop_event = None
        self.app = None

    def start(self):
        if self.running or self._thread is not None:
            return

        def target():
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self.app = self.app_factory()
                self._loop.run_until_complete(self._run_server())
            except Exception as exc:
                self.running = False
                self._thread = None
                self._loop = None
                self.app = None
                print("Server startup failed:", exc)
                push_gui_update(status=f"Startup failed: {exc}", address=f"http://{self.host}:{self.port}")

        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def stop(self):
        if not self.running or self._loop is None or self._stop_event is None:
            return
        self._loop.call_soon_threadsafe(self._stop_event.set)

    async def _run_server(self):
        try:
            self._runner = web.AppRunner(self.app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.host, self.port)
            await self._site.start()
        except OSError as exc:
            if getattr(exc, "errno", None) in {10048, 98, 13}:
                fallback_port = self.port + 1
                self.port = fallback_port
                push_gui_update(status="Port busy, retrying", address=f"http://{self.host}:{self.port}")
                self._runner = web.AppRunner(self.app)
                await self._runner.setup()
                self._site = web.TCPSite(self._runner, self.host, self.port)
                await self._site.start()
            else:
                raise

        self.running = True
        push_gui_update(status="Running", address=f"http://{self.host}:{self.port}")
        self._stop_event = asyncio.Event()
        await self._stop_event.wait()
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        self.app = None
        self.running = False
        self._site = None
        self._runner = None
        self._stop_event = None
        self._thread = None
        self._loop = None
        push_gui_update(status="Stopped", address=f"http://{self.host}:{self.port}")


def push_gui_update(**kwargs):
    if not GUI_ENABLED:
        return
    with GUI_LOCK:
        GUI_STATE.update(kwargs)
    try:
        GUI_QUEUE.put_nowait(kwargs)
    except Exception:
        pass


def toggle_server_state():
    global SERVER_CONTROLLER
    if SERVER_CONTROLLER is None:
        return
    if SERVER_CONTROLLER.running:
        SERVER_CONTROLLER.stop()
        push_gui_update(status="Stopped")
    else:
        SERVER_CONTROLLER.start()
        push_gui_update(status="Starting")


def toggle_window_visibility():
    global GUI_ROOT
    if GUI_ROOT is None or not GUI_ROOT.winfo_exists():
        return
    if GUI_ROOT.state() == "withdrawn":
        GUI_ROOT.deiconify()
        GUI_ROOT.lift()
    else:
        GUI_ROOT.withdraw()


def stop_and_exit():
    global GUI_ROOT, TRAY_ICON
    if SERVER_CONTROLLER is not None:
        SERVER_CONTROLLER.stop()
    if GUI_ROOT is not None and GUI_ROOT.winfo_exists():
        GUI_ROOT.destroy()
    if TRAY_ICON is not None:
        TRAY_ICON.stop()
    raise SystemExit(0)


def get_tray_icon_image():
    if Image is None:
        return None
    candidates = [
        BASE_DIR / "icons" / "favicon.ico",
        BASE_DIR / "icons" / "android-chrome-192x192.png",
        BASE_DIR / "icons" / "apple-touch-icon.png",
    ]
    for icon_path in candidates:
        try:
            if not icon_path.exists():
                continue
            image = Image.open(icon_path)
            if image.mode != "RGBA":
                image = image.convert("RGBA")
            return image.resize((64, 64))
        except Exception:
            continue
    try:
        return Image.new("RGBA", (64, 64), color=(22, 119, 255))
    except Exception:
        return None


def start_gui():
    global GUI_THREAD, GUI_ROOT
    if not GUI_ENABLED or tk is None:
        return

    def run():
        global GUI_ROOT, TRAY_ICON
        root = tk.Tk()
        GUI_ROOT = root
        root.title("Remote Access")
        icon_path = BASE_DIR / "icons" / "favicon.ico"
        if icon_path.exists():
            try:
                root.iconbitmap(str(icon_path))
            except Exception:
                pass
        root.geometry("400x360")
        root.resizable(False, False)
        root.configure(bg="#121212")
        root.columnconfigure(0, weight=1)

        status_var = tk.StringVar(value="Status: Ready")
        address_var = tk.StringVar(value="Address: http://0.0.0.0:8080")
        client_var = tk.StringVar(value="No client connected")
        connections_var = tk.StringVar(value="0")
        start_stop_var = tk.StringVar(value="Start")
        browser_var = tk.StringVar(value="Browser: —")
        platform_var = tk.StringVar(value="Platform: —")
        ip_var = tk.StringVar(value="IP: —")
        location_var = tk.StringVar(value="Location: —")
        screen_var = tk.StringVar(value="Screen: —")
        timezone_var = tk.StringVar(value="Timezone: —")
        geo_var = tk.StringVar(value="Geo: —")

        header_frame = tk.Frame(root, bg="#1b1b1b", bd=0)
        header_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 6))
        status_label = tk.Label(
            header_frame,
            textvariable=status_var,
            bg="#1b1b1b",
            fg="#7fffd4",
            font=("Segoe UI", 12, "bold"),
            anchor="w"
        )
        status_label.pack(fill="x")

        info_frame = tk.Frame(root, bg="#181818", bd=1, relief="solid")
        info_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=6)
        info_frame.columnconfigure(0, weight=1)
        info_frame.columnconfigure(1, weight=1)

        tk.Label(info_frame, textvariable=address_var, bg="#181818", fg="#f5f5f5", anchor="w", padx=8, pady=4).grid(row=0, column=0, sticky="ew", padx=4, pady=2)
        tk.Label(info_frame, textvariable=connections_var, bg="#181818", fg="#f5f5f5", anchor="w", padx=8, pady=4).grid(row=0, column=1, sticky="ew", padx=4, pady=2)
        tk.Label(info_frame, textvariable=client_var, bg="#181818", fg="#f5f5f5", anchor="w", padx=8, pady=4).grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=2)
        info_frame.configure(padx=4, pady=4)

        control_frame = tk.Frame(root, bg="#121212")
        control_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))
        control_frame.columnconfigure(0, weight=1)

        button = tk.Button(control_frame, textvariable=start_stop_var, command=toggle_server_state, bg="#2f6fed", fg="white", padx=10, pady=10, font=("Segoe UI", 12, "bold"))
        button.grid(row=0, column=0, sticky="ew")

        instruction_label = tk.Label(control_frame, text="Start the server to accept remote clients.", bg="#121212", fg="#c0c0c0", anchor="center", font=("Segoe UI", 9))
        instruction_label.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        card_frame = tk.Frame(root, bg="#1f1f1f", bd=1, relief="solid")
        card_frame.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=6)
        card_frame.columnconfigure(0, weight=1)

        tk.Label(card_frame, text="Connected Client Details", bg="#1f1f1f", fg="#f5f5f5", font=("Segoe UI", 11, "bold"), anchor="w", padx=12, pady=10).grid(row=0, column=0, sticky="ew")
        tk.Label(card_frame, textvariable=browser_var, bg="#1f1f1f", fg="#d0d0d0", anchor="w", padx=12, pady=2).grid(row=1, column=0, sticky="ew")
        tk.Label(card_frame, textvariable=platform_var, bg="#1f1f1f", fg="#d0d0d0", anchor="w", padx=12, pady=2).grid(row=2, column=0, sticky="ew")
        tk.Label(card_frame, textvariable=ip_var, bg="#1f1f1f", fg="#d0d0d0", anchor="w", padx=12, pady=2).grid(row=3, column=0, sticky="ew")
        tk.Label(card_frame, textvariable=location_var, bg="#1f1f1f", fg="#d0d0d0", anchor="w", padx=12, pady=2).grid(row=4, column=0, sticky="ew")
        tk.Label(card_frame, textvariable=screen_var, bg="#1f1f1f", fg="#d0d0d0", anchor="w", padx=12, pady=2).grid(row=5, column=0, sticky="ew")
        tk.Label(card_frame, textvariable=timezone_var, bg="#1f1f1f", fg="#d0d0d0", anchor="w", padx=12, pady=2).grid(row=6, column=0, sticky="ew")
        tk.Label(card_frame, textvariable=geo_var, bg="#1f1f1f", fg="#d0d0d0", anchor="w", padx=12, pady=2).grid(row=7, column=0, sticky="ew", pady=(2, 10))

        root.rowconfigure(3, weight=1)

        def update_button_label():
            if SERVER_CONTROLLER is not None and SERVER_CONTROLLER.running:
                start_stop_var.set("Stop")
            else:
                start_stop_var.set("Start")

        def format_metadata(metadata):
            if not metadata:
                return "No client metadata yet."
            lines = []
            browser = metadata.get("browser", "unknown")
            platform = metadata.get("platform", "unknown")
            mobile = metadata.get("isMobile", False)
            ip = metadata.get("ipAddress") or "unknown"
            location = metadata.get("location") or {}
            city = location.get("city") or "unknown"
            region = location.get("region") or ""
            country = location.get("country") or ""
            screen = metadata.get("screen") or {}
            width = screen.get("width")
            height = screen.get("height")
            tz = metadata.get("timezone") or "unknown"
            geo = metadata.get("geoPermission", "unknown")
            lines.append(f"Browser: {browser}")
            lines.append(f"Platform: {platform}")
            lines.append(f"Mobile: {mobile}")
            lines.append(f"IP: {ip}")
            lines.append(f"Location: {city}{', ' + region if region else ''}{', ' + country if country else ''}")
            lines.append(f"Screen: {width}x{height}" if width and height else "Screen: unknown")
            lines.append(f"Timezone: {tz}")
            lines.append(f"Geo: {geo}")
            return "\n".join(lines)

        def refresh():
            try:
                while True:
                    payload = GUI_QUEUE.get_nowait()
                    if "status" in payload:
                        status_var.set(f"Status: {payload['status']}")
                    if "address" in payload:
                        address_var.set(f"Address: {payload['address']}")
                    if "last_client" in payload:
                        client_var.set(f"Client: {payload['last_client']}")
                    if "connections" in payload:
                        connections_var.set(f"Connections: {payload['connections']}")
                    if "last_metadata" in payload:
                        metadata = payload["last_metadata"] or {}
                        browser_var.set(f"Browser: {metadata.get('browser', '—')}")
                        platform_var.set(f"Platform: {metadata.get('platform', '—')}")
                        ip_var.set(f"IP: {metadata.get('ipAddress', '—')}")
                        location = metadata.get('location') or {}
                        location_str = ", ".join(filter(None, [location.get('city'), location.get('region'), location.get('country')]))
                        location_var.set(f"Location: {location_str or '—'}")
                        screen = metadata.get('screen') or {}
                        screen_var.set(f"Screen: {screen.get('width', '—')}x{screen.get('height', '—')}")
                        timezone_var.set(f"Timezone: {metadata.get('timezone', '—')}")
                        geo_var.set(f"Geo: {metadata.get('geoPermission', '—')}")
            except queue.Empty:
                pass
            update_button_label()
            root.after(150, refresh)

        if pystray is not None:
            try:
                image = get_tray_icon_image()
                if image is None:
                    raise RuntimeError("No tray icon available")
                menu = pystray.Menu(
                    pystray.MenuItem("Show/Hide", toggle_window_visibility),
                    pystray.MenuItem("Start/Stop", toggle_server_state),
                    pystray.MenuItem("Exit", stop_and_exit),
                )
                TRAY_ICON = pystray.Icon("remote_access", image, "Remote Access", menu)
                threading.Thread(target=TRAY_ICON.run, daemon=True).start()
            except Exception as exc:
                print("Tray icon unavailable:", exc)

        root.protocol("WM_DELETE_WINDOW", lambda: root.withdraw())
        root.after(150, refresh)
        root.mainloop()

    try:
        run()
    except Exception as exc:
        print("GUI unavailable:", exc)
        push_gui_update(status="GUI unavailable")


def persist_client_metadata(metadata):
    try:
        history = []
        if CLIENT_METADATA_FILE.exists():
            with open(CLIENT_METADATA_FILE, "r", encoding="utf-8") as handle:
                history = json.load(handle)
        if not isinstance(history, list):
            history = []

        history.append(metadata)
        with open(CLIENT_METADATA_FILE, "w", encoding="utf-8") as handle:
            json.dump(history[-20:], handle, indent=2)
    except Exception as exc:
        print("Failed to persist client metadata:", exc)


async def hold_key_task(key_stroke):
    try:
        while True:
            pydirectinput.keyDown(key_stroke)
            await asyncio.sleep(0.03)
    except asyncio.CancelledError:
        pydirectinput.keyUp(key_stroke)

async def hold_rotation_task(direction):
    pydirectinput.mouseDown(button='right')
    step = 25 if direction == "right" else -25
    try:
        while True:
            pydirectinput.moveRel(step, 0, relative=True)
            await asyncio.sleep(0.016)
    except asyncio.CancelledError:
        pydirectinput.mouseUp(button='right')

async def hold_zoom_task(direction):
    clicks = 1 if direction == "in" else -1
    try:
        while True:
            native_win32_scroll(clicks)
            await asyncio.sleep(0.03)
    except asyncio.CancelledError:
        pass

async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    pc = RTCPeerConnection()

    video_track = ScreenCaptureTrack()
    pc.addTrack(video_track)

    if player.audio:
        pc.addTrack(player.audio)

    for transceiver in pc.getTransceivers():
        if transceiver.kind == "video":
            transceiver.sender.contentHint = "detail"
            capabilities = RTCRtpSender.getCapabilities("video")
            vp8 = [c for c in capabilities.codecs if c.name == "VP8"]
            if vp8:
                transceiver.setCodecPreferences(vp8)

    push_gui_update(status="Connected", connections=1)

    @pc.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(message):
            # Fire and forget scheduling to ensure instant execution processing loop profiles
            asyncio.create_task(process_input_message(message, video_track.monitor))

    async def process_input_message(message, monitor):
        try:
            data = json.loads(message)

            if data["type"] == "mousemove":
                dx = int(data.get("dx", 0))
                dy = int(data.get("dy", 0))
                if dx != 0 or dy != 0:
                    send_hardware_input(MOUSEEVENTF_MOVE, dx, dy)

            elif data["type"] == "client_info":
                metadata = data.get("metadata") or data
                print("Client metadata received:")
                print(json.dumps(metadata, indent=2))
                persist_client_metadata(metadata)
                client_label = metadata.get("browser", "unknown")
                if metadata.get("ipAddress"):
                    client_label = f"{client_label} ({metadata['ipAddress']})"
                push_gui_update(last_client=client_label, last_metadata=metadata)

            elif data["type"] == "mousestart":
                btn = data["button"]
                down_flag = MOUSEEVENTF_LEFTDOWN if btn == 0 else (MOUSEEVENTF_RIGHTDOWN if btn == 1 else MOUSEEVENTF_MIDDLEDOWN)
                send_hardware_input(down_flag, 0, 0)

            elif data["type"] == "mouseend":
                btn = data["button"]
                up_flag = MOUSEEVENTF_LEFTUP if btn == 0 else (MOUSEEVENTF_RIGHTUP if btn == 1 else MOUSEEVENTF_MIDDLEUP)
                send_hardware_input(up_flag, 0, 0)

            elif data["type"] == "scroll":
                clicks = 1 if data["steps"] > 0 else -1
                native_win32_scroll(clicks)

            elif data["type"] == "btnstart":
                k = data["key"]
                if k in active_loops:
                    active_loops[k].cancel()
                
                if k in ["up", "down", "left", "right"]:
                    active_loops[k] = asyncio.create_task(hold_key_task(k))
                elif k in ["rotleft", "rotright"]:
                    active_loops[k] = asyncio.create_task(hold_rotation_task("left" if k == "rotleft" else "right"))
                elif k == "zoomin":
                    active_loops[k] = asyncio.create_task(hold_zoom_task("in"))
                elif k == "zoomout":
                    active_loops[k] = asyncio.create_task(hold_zoom_task("out"))

            elif data["type"] == "btnend":
                k = data["key"]
                if k in active_loops:
                    active_loops[k].cancel()
                    del active_loops[k]
                if k in ["up", "down", "left", "right"]:
                    pydirectinput.keyUp(k)
                if k in ["rotleft", "rotright"]:
                    pydirectinput.mouseUp(button='right')

            elif data["type"] == "keyboard":
                val = data["text"]
                if val == "backspace":
                    keyboard.press(Key.backspace)
                    keyboard.release(Key.backspace)
                elif val == "escape":
                    keyboard.press(Key.esc)
                    keyboard.release(Key.esc)
                else:
                    keyboard.type(val)

        except Exception as e:
            print("Input Task Run Error:", e)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        if pc.connectionState in ["failed", "closed"]:
            push_gui_update(status="Disconnected")
            await pc.close()

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.05)

    return web.Response(
        content_type="application/json",
        text=json.dumps({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})
    )

def create_app():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/offer", offer)
    app.router.add_static("/icons/", path=str(BASE_DIR / "icons"), name="icons")
    return app

if __name__ == "__main__":
    SERVER_CONTROLLER = RemoteAccessServerController(create_app)
    push_gui_update(status="Ready", address=f"http://{SERVER_CONTROLLER.host}:{SERVER_CONTROLLER.port}")
    if GUI_ENABLED and tk is not None:
        start_gui()
    else:
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass
