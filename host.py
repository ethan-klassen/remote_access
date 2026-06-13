import asyncio
import json
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.rtcrtpsender import RTCRtpSender
from av import VideoFrame
import mss
import pyautogui

pyautogui.PAUSE = 0

class ScreenCaptureTrack(VideoStreamTrack):
    """Custom WebRTC video track that captures the PC monitor in YUV420p."""
    def __init__(self):
        super().__init__()
        self.sct = mss.MSS()
        self.monitor = self.sct.monitors[1]  # Target primary monitor explicitly

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        
        # Capture desktop screenshot
        img = self.sct.grab(self.monitor)
        
        # FIX: Build from raw BGRA and re-mux directly into standard YUV420P format
        # This allows mobile hardcoded decoders to actually process the image array.
        bgra_frame = VideoFrame.from_ndarray(img.raw, format="bgra")
        yuv_frame = bgra_frame.reformat(width=self.monitor["width"], height=self.monitor["height"], format="yuv420p")
        
        yuv_frame.pts = pts
        yuv_frame.time_base = time_base
        
        # Lock to 20 FPS to protect network bandwidth
        await asyncio.sleep(1 / 20)
        return yuv_frame

async def handle_index(request):
    return web.FileResponse('index.html')

async def handle_offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    
    video_track = ScreenCaptureTrack()
    sender = pc.addTrack(video_track)

    # Force standard H264 baseline profiles that match mobile system hardware
    capabilities = RTCRtpSender.getCapabilities("video")
    h264_codecs = [c for c in capabilities.codecs if c.name == "H264"]
    if h264_codecs:
        sender.setCodecPreferences(h264_codecs)

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
                print(f"Input processing failure: {e}")

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
