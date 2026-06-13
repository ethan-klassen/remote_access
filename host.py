import asyncio
import fractions
import json
import time
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
import av
import mss
import pyautogui

pyautogui.PAUSE = 0

class ScreenCaptureTrack(VideoStreamTrack):
    """Custom WebRTC video track that captures the PC monitor using real wall-clock timestamps."""
    def __init__(self):
        super().__init__()
        self.sct = mss.MSS()
        
        # Grab primary display configuration dictionary elements cleanly
        raw_monitor = self.sct.monitors[1]  
        
        # Enforce divisible-by-16 structural limitations for mobile graphics hardware
        self.width = (raw_monitor["width"] // 16) * 16
        self.height = (raw_monitor["height"] // 16) * 16
        
        self.monitor = {
            "top": raw_monitor["top"],
            "left": raw_monitor["left"],
            "width": self.width,
            "height": self.height
        }
        
        # FIX: Explicitly assign a static tracking identity to prevent Android metadata drops
        self._id = "video-stream"
        
        # Track initial start execution baseline clock
        self.start_time = time.time()
        print(f"Streaming activated using track metadata pairing at: {self.width}x{self.height}")

    async def recv(self):
        current_now = time.time()
        elapsed_seconds = current_now - self.start_time
        
        # Convert true wall-clock interval ticks to a 90kHz WebRTC baseline video scale
        pts = int(elapsed_seconds * 90000)
        
        # Grab live screen viewport capture buffer
        img = self.sct.grab(self.monitor)
        
        # Explicitly allocate memory spaces for both BGRA and YUV formats.
        bgra_frame = av.VideoFrame(self.width, self.height, format="bgra")
        bgra_frame.planes[0].update(img.bgra)  # Update internal array plane directly
        
        # Convert the built memory structure safely into mobile-compliant YUV420P
        yuv_frame = bgra_frame.reformat(width=self.width, height=self.height, format="yuv420p")
        
        # Explicitly declare true timeline attributes onto the outgoing track frame container
        yuv_frame.pts = pts
        yuv_frame.time_base = fractions.Fraction(1, 90000)
        
        # Lock pacing frequency to ~25 frames per second to limit throughput bottlenecks
        await asyncio.sleep(1 / 25)
        return yuv_frame

async def handle_index(request):
    return web.FileResponse('index.html')

async def handle_offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    video_track = ScreenCaptureTrack()
    
    # Standard track addition sequence
    pc.addTrack(video_track)

    @pc.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(message):
            try:
                data = json.loads(message)
                if data["type"] == "mousemove":
                    monitor = video_track.monitor
                    target_x = int(data["x"] * monitor["width"]) + monitor["left"]
                    target_y = int(data["y"] * monitor["height"]) + monitor["top"]
                    pyautogui.moveTo(target_x, target_y)
                elif data["type"] == "click":
                    pyautogui.click()
            except Exception as e:
                print(f"Data channel track parsing drop error: {e}")

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
