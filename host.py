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
        print(f"High-Fidelity 1080p Engine Active: {self.width}x{self.height}")

    async def recv(self):
        current_now = time.time()
        elapsed_seconds = current_now - self.start_time
        pts = int(elapsed_seconds * 90000)
        
        # Capture monitor desktop buffer
        img = self.sct.grab(self.monitor)
        
        # Build raw BGRA surface frames
        bgra_frame = av.VideoFrame(self.width, self.height, format="bgra")
        bgra_frame.planes[0].update(img.bgra)
        
        # FIX: Added 'lanczos' interpolation to make fine text elements, lines, and details 
        # look razor-sharp on your iPad retina screen.
        yuv_frame = bgra_frame.reformat(
            width=self.width, 
            height=self.height, 
            format="yuv420p", 
            interpolation="LANCZOS"
        )
        yuv_frame.pts = pts
        yuv_frame.time_base = fractions.Fraction(1, 90000)
        
        await asyncio.sleep(1 / 30)  # Smooth 30 FPS pacing execution sequence
        return yuv_frame

async def handle_index(request):
    return web.FileResponse('index.html')

async def handle_offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    video_track = ScreenCaptureTrack()
    pc.addTrack(video_track)

    # Force standard hardware-friendly H.264 profiles for optimal Apple/iOS decoding
    for transceiver in pc.getTransceivers():
        if transceiver.kind == "video":
            transceiver.sender.contentHint = "detail"  # Prioritize text sharpness over fluid motion
            capabilities = RTCRtpSender.getCapabilities("video")
            h264_codecs = [c for c in capabilities.codecs if c.name == "H264"]
            if h264_codecs:
                transceiver.setCodecPreferences(h264_codecs)

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
    
    # FIX: Cleaned and bulletproofed the SDP text manipulation block to prevent crashes.
    # It extracts the payload ID correctly and injects high-quality bitrate tags safely.
    sdp_lines = answer.sdp.split("\r\n")
    modified_sdp = []
    
    for line in sdp_lines:
        modified_sdp.append(line)
        if line.startswith("a=rtpmap:") and "H264" in line:
            try:
                # Safely split out the payload ID: "a=rtpmap:102 H264/90000" -> "102"
                payload_id = line.split(":")[1].split()[0]
                modified_sdp.append(f"a=fmtp:{payload_id} profile-level-id=42e01f;level-asymmetry-allowed=1;packetization-mode=1;x-google-start-bitrate=6000;x-google-max-bitrate=8000;x-google-min-bitrate=3000")
            except Exception as e:
                print(f"SDP optimization error skipped: {e}")
    
    answer.sdp = "\r\n".join(modified_sdp)
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
