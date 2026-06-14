import asyncio
import fractions
import json
import time
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.rtcrtpsender import RTCRtpSender
import av
import mss
import pyautogui

# FIX: Set the global VP8 software engine to a crisp baseline bitrate profile
import aiortc.codecs.vpx
aiortc.codecs.vpx.DEFAULT_BITRATE = 6000000

pyautogui.PAUSE = 0

class ScreenCaptureTrack(VideoStreamTrack):
    """High-quality WebRTC video track that captures the PC monitor at native 1080p resolution."""
    def __init__(self):
        super().__init__()
        self.sct = mss.MSS()
        raw_monitor = self.sct.monitors[1]  # Target primary monitor directly out of the list
        
        # Enforce divisible-by-16 dimensional boundaries for hardware decoder compatibility
        self.width = (raw_monitor["width"] // 16) * 16
        self.height = (raw_monitor["height"] // 16) * 16
        
        self.monitor = {
            "top": raw_monitor["top"],
            "left": raw_monitor["left"],
            "width": self.width,
            "height": self.height
        }
        
        self._id = "video-stream"
        self.start_time = time.time()
        print(f"Universal High-Quality Engine Active: {self.width}x{self.height}")

    async def recv(self):
        current_now = time.time()
        elapsed_seconds = current_now - self.start_time
        pts = int(elapsed_seconds * 90000)
        
        # Capture monitor desktop buffer
        img = self.sct.grab(self.monitor)
        
        # Build raw BGRA surface frames
        bgra_frame = av.VideoFrame(self.width, self.height, format="bgra")
        bgra_frame.planes[0].update(img.bgra)
        
        # Convert to standardized YUV420p format using fast bilinear matrix mapping.
        yuv_frame = bgra_frame.reformat(
            width=self.width, 
            height=self.height, 
            format="yuv420p", 
            interpolation="BILINEAR"
        )
        yuv_frame.pts = pts
        yuv_frame.time_base = fractions.Fraction(1, 90000)
        
        await asyncio.sleep(1 / 30)  # Stable 30 FPS pacing execution sequence
        return yuv_frame

async def handle_index(request):
    return web.FileResponse('index.html')

async def handle_offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    video_track = ScreenCaptureTrack()
    pc.addTrack(video_track)

    # FIX: Explicitly loop through the active transceivers to prioritize VP8 profiles.
    # This prevents the computer from choosing broken H.264 parameters and keeps video rendering working.
    for transceiver in pc.getTransceivers():
        if transceiver.kind == "video":
            transceiver.sender.contentHint = "detail"  # Prioritize text sharpness universally
            capabilities = RTCRtpSender.getCapabilities("video")
            vp8_codecs = [c for c in capabilities.codecs if c.name == "VP8"]
            if vp8_codecs:
                transceiver.setCodecPreferences(vp8_codecs)

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
            except Exception:
                pass

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
