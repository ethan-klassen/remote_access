import asyncio
import fractions
import json
import time
import os

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

# Use pydirectinput for hardware level DirectX scancodes
import pydirectinput
pydirectinput.PAUSE = 0.0
pydirectinput.FAILSAFE = False

# Keep pynput ONLY for complex keyboard text strings
from pynput.keyboard import Controller as KeyController, Key
keyboard = KeyController()

# Set high VP8 encoding bitrate
import aiortc.codecs.vpx
aiortc.codecs.vpx.DEFAULT_BITRATE = 6000000

# System audio capture
player = MediaPlayer(
    "audio=CABLE Output (VB-Audio Virtual Cable)",
    format="dshow",
    options={
        "audio_buffer_size": "20"
    }
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
        print(f"Streaming desktop at {self.width}x{self.height}")

    async def recv(self):
        await asyncio.sleep(1 / 30) # Maintain ~30FPS pace
        
        elapsed = time.time() - self.start_time
        pts = int(elapsed * 90000)

        img = self.sct.grab(self.monitor)

        # High performance raw byte memory buffer allocation
        frame = av.VideoFrame(self.width, self.height, format="bgra")
        frame.planes[0].update(img.bgra)

        frame.pts = pts
        frame.time_base = fractions.Fraction(1, 90000)
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
    step = 20 if direction == "right" else -20
    try:
        while True:
            pydirectinput.moveRel(step, 0, relative=True)
            await asyncio.sleep(0.015)
    except asyncio.CancelledError:
        pydirectinput.mouseUp(button='right')

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
        # Use an asynchronous event processor to prevent task loop crashes
        @channel.on("message")
        async def on_message(message):
            try:
                data = json.loads(message)
                monitor = video_track.monitor

                if "x" in data and "y" in data:
                    target_x = int(data["x"] * monitor["width"]) + monitor["left"]
                    target_y = int(data["y"] * monitor["height"]) + monitor["top"]

                if data["type"] == "mousemove":
                    pydirectinput.moveTo(target_x, target_y)

                elif data["type"] == "mousestart":
                    pydirectinput.moveTo(target_x, target_y)
                    btn_type = 'left' if data["button"] == 0 else ('right' if data["button"] == 1 else 'middle')
                    pydirectinput.mouseDown(button=btn_type)

                elif data["type"] == "mouseend":
                    btn_type = 'left' if data["button"] == 0 else ('right' if data["button"] == 1 else 'middle')
                    pydirectinput.mouseUp(button=btn_type)

                elif data["type"] == "scroll":
                    pydirectinput.scroll(int(data["steps"]))

                elif data["type"] == "btnstart":
                    k = data["key"]
                    if k in active_loops:
                        active_loops[k].cancel()
                    
                    if k in ["up", "down", "left", "right"]:
                        active_loops[k] = asyncio.create_task(hold_key_task(k))
                    elif k in ["rotleft", "rotright"]:
                        active_loops[k] = asyncio.create_task(hold_rotation_task("left" if k == "rotleft" else "right"))
                    elif k == "zoomin":
                        pydirectinput.scroll(3)
                    elif k == "zoomout":
                        pydirectinput.scroll(-3)

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
                    else:
                        keyboard.type(val)

            except Exception as e:
                print("WebRTC Input Error:", e)

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
