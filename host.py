import asyncio
import fractions
import json
import time
import os
import ctypes
from ctypes import wintypes

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
    return web.FileResponse("index.html")

active_loops = {}

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

app = web.Application()
app.router.add_get("/", index)
app.router.add_post("/offer", offer)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8080)
