import asyncio
import json
import logging
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame
import mss
import pyautogui

# Disable PyAutoGUI delay for lower latency
pyautogui.PAUSE = 0

class ScreenCaptureTrack(VideoStreamTrack):
    """Custom WebRTC video track that captures the PC monitor."""
    def __init__(self):
        super().__init__()
        # FIX: Changed from mss.mss() to mss.MSS() to fix deprecation warning
        self.sct = mss.MSS()
        self.monitor = self.sct.monitors[1] # Targets primary monitor

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        
        # Capture raw pixels from screen
        img = self.sct.grab(self.monitor)
        
        # Convert raw BGRA pixels to video frame
        frame = VideoFrame.from_ndarray(img.raw, format="bgra")
        frame.pts = pts
        frame.time_base = time_base
        return frame

async def handle_index(request):
    """Serves the mobile phone interface file."""
    return web.FileResponse('index.html')

async def handle_offer(request):
    """Handles the WebRTC handshake (SDP Exchange) and mouse data channel."""
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    
    # Track setup
    video_track = ScreenCaptureTrack()
    pc.addTrack(video_track)

    @pc.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(message):
            data = json.loads(message)
            if data["type"] == "mousemove":
                monitor = video_track.monitor
                target_x = int(data["x"] * monitor["width"]) + monitor["left"]
                target_y = int(data["y"] * monitor["height"]) + monitor["top"]
                pyautogui.moveTo(target_x, target_y)
            elif data["type"] == "click":
                pyautogui.click()

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.Response(
        content_type="application/json",
        text=json.dumps({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})
    )

if __name__ == "__main__":
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_post("/offer", handle_offer)
    web.run_app(app, host="0.0.0.0", port=8080)
