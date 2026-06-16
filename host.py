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

# Import controllers
from pynput.mouse import Controller as MouseController, Button
from pynput.keyboard import Controller as KeyController, Key
mouse = MouseController()
keyboard = KeyController()

import aiortc.codecs.vpx
aiortc.codecs.vpx.DEFAULT_BITRATE = 6000000

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

    async def recv(self):
        elapsed = time.time() - self.start_time
        pts = int(elapsed * 90000)
        img = self.sct.grab(self.monitor)
        bgra = av.VideoFrame(self.width, self.height, format="bgra")
        bgra.planes[0].update(img.bgra)
        frame = bgra.reformat(width=self.width, height=self.height, format="yuv420p", interpolation="BILINEAR")
        frame.pts = pts
        frame.time_base = fractions.Fraction(1, 90000)
        await asyncio.sleep(1 / 30)
        return frame


async def index(request):
    return web.FileResponse("index.html")

async def manifest(request):
    return web.json_response({
        "short_name": "RemoteSims", "name": "Remote PC Client",
        "start_url": "/", "background_color": "#000000",
        "theme_color": "#000000", "display": "standalone", "orientation": "landscape"
    })


# Task loop handlers for held buttons
active_loops = {}

async def hold_key_task(key_stroke):
    while True:
        keyboard.press(key_stroke)
        await asyncio.sleep(0.03)

async def hold_rotation_task(direction):
    # Lock middle click down and shake mouse relatively to turn camera stable
    mouse.press(Button.middle)
    step = 15 if direction == "right" else -15
    while True:
        curr_x, curr_y = mouse.position
        mouse.position = (curr_x + step, curr_y)
        await asyncio.sleep(0.02)


async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    pc = RTCPeerConnection()
    video_track = ScreenCaptureTrack()
    pc.addTrack(video_track)

    if player.audio:
        pc.addTrack(player.audio)

    @pc.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(message):
            try:
                data = json.loads(message)
                
                # 1. Base Touch Triggers
                if "x" in data and "y" in data:
                    monitor = video_track.monitor
                    target_x = int(data["x"] * monitor["width"]) + monitor["left"]
                    target_y = int(data["y"] * monitor["height"]) + monitor["top"]

                if data["type"] == "mousemove":
                    mouse.position = (target_x, target_y)
                elif data["type"] == "mousestart":
                    mouse.position = (target_x, target_y)
                    mouse.press(Button.left)
                elif data["type"] == "mouseend":
                    mouse.release(Button.left)
                elif data["type"] == "scroll":
                    mouse.scroll(0, int(data["steps"]))

                # 2. Virtual Side Buttons Processing
                elif data["type"] == "btnstart":
                    k = data["key"]
                    if k == "up": active_loops[k] = asyncio.create_task(hold_key_task(Key.up))
                    elif k == "down": active_loops[k] = asyncio.create_task(hold_key_task(Key.down))
                    elif k == "left": active_loops[k] = asyncio.create_task(hold_key_task(Key.left))
                    elif k == "right": active_loops[k] = asyncio.create_task(hold_key_task(Key.right))
                    elif k == "rotleft": active_loops[k] = asyncio.create_task(hold_rotation_task("left"))
                    elif k == "rotright": active_loops[k] = asyncio.create_task(hold_rotation_task("right"))
                    elif k == "zoomin": mouse.scroll(0, 3)
                    elif k == "zoomout": mouse.scroll(0, -3)

                elif data["type"] == "btnend":
                    k = data["key"]
                    if k in active_loops:
                        active_loops[k].cancel()
                        del active_loops[k]
                    if k in ["up", "down", "left", "right"]:
                        mapping = {"up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right}
                        keyboard.release(mapping[k])
                    if k in ["rotleft", "rotright"]:
                        mouse.release(Button.middle)

                # 3. Direct Keyboard Strings Input
                elif data["type"] == "keyboard":
                    val = data["text"]
                    if val == "backspace":
                        keyboard.press(Key.backspace)
                        keyboard.release(Key.backspace)
                    else:
                        keyboard.type(val)

            except Exception as e:
                print("Input processing failure:", e)

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
app.router.add_get("/manifest.json", manifest)
app.router.add_post("/offer", offer)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8080)
