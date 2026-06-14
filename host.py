import asyncio
import fractions
import json
import time
import threading
import traceback
import numpy as np
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, MediaStreamTrack
from aiortc.rtcrtpsender import RTCRtpSender
import av
import mss
import pyautogui
import pyaudiowpatch as pyaudio

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

class SystemAudioTrack(MediaStreamTrack):
    """WebRTC audio track that captures system audio via WASAPI loopback."""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._queue = asyncio.Queue(maxsize=50)
        self._loop = None
        self._start_time = None
        self._sample_count = 0

        # Audio format constants
        self.SAMPLE_RATE = 48000
        self.CHANNELS = 2
        self.SAMPLES_PER_FRAME = 960  # 20ms at 48kHz

        # Find and open the WASAPI loopback device
        self._pa = pyaudio.PyAudio()
        loopback_device = self._find_loopback_device()
        if loopback_device is None:
            raise RuntimeError("No WASAPI loopback device found. Cannot capture system audio.")

        device_info = loopback_device
        print(f"Audio Loopback Device: {device_info['name']}")

        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self.CHANNELS,
            rate=self.SAMPLE_RATE,
            input=True,
            input_device_index=device_info['index'],
            frames_per_buffer=self.SAMPLES_PER_FRAME,
        )

        # Start the capture thread
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def _find_loopback_device(self):
        """Find the default WASAPI loopback device."""
        try:
            wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError:
            return None

        default_speakers_idx = wasapi_info['defaultOutputDevice']
        default_speakers = self._pa.get_device_info_by_index(default_speakers_idx)

        # Search for the matching loopback device
        for i in range(self._pa.get_device_count()):
            dev = self._pa.get_device_info_by_index(i)
            if dev.get('isLoopbackDevice', False) and dev['name'].startswith(default_speakers['name'].split(' (')[0]):
                return dev

        # Fallback: return any loopback device
        for i in range(self._pa.get_device_count()):
            dev = self._pa.get_device_info_by_index(i)
            if dev.get('isLoopbackDevice', False):
                return dev

        return None

    def _capture_loop(self):
        """Background thread that reads audio and pushes frames to the async queue."""
        while self._running:
            try:
                data = self._stream.read(self.SAMPLES_PER_FRAME, exception_on_overflow=False)
                if self._loop is not None:
                    try:
                        self._loop.call_soon_threadsafe(self._queue.put_nowait, data)
                    except asyncio.QueueFull:
                        pass  # Drop frame if consumer is behind
            except Exception as e:
                print(f"Audio capture error: {e}")
                break

    def _make_silent_frame(self):
        """Generate a silent audio frame as a fallback."""
        silent = np.zeros((self.CHANNELS, self.SAMPLES_PER_FRAME), dtype=np.int16)
        frame = av.AudioFrame.from_ndarray(silent, format='s16', layout='stereo')
        frame.sample_rate = self.SAMPLE_RATE
        frame.pts = self._sample_count
        frame.time_base = fractions.Fraction(1, self.SAMPLE_RATE)
        self._sample_count += self.SAMPLES_PER_FRAME
        return frame

    async def recv(self):
        if self._loop is None:
            self._loop = asyncio.get_event_loop()
            self._start_time = time.time()

        try:
            data = await asyncio.wait_for(self._queue.get(), timeout=1.0)

            # Convert raw bytes to numpy: interleaved stereo → (channels, samples)
            raw = np.frombuffer(data, dtype=np.int16)
            samples = len(raw) // self.CHANNELS
            audio_array = raw.reshape((samples, self.CHANNELS)).T

            frame = av.AudioFrame.from_ndarray(audio_array, format='s16', layout='stereo')
            frame.sample_rate = self.SAMPLE_RATE
            frame.pts = self._sample_count
            frame.time_base = fractions.Fraction(1, self.SAMPLE_RATE)
            self._sample_count += samples

            return frame
        except asyncio.TimeoutError:
            return self._make_silent_frame()
        except Exception as e:
            print(f"Audio recv error: {e}")
            return self._make_silent_frame()

    def stop(self):
        super().stop()
        self._running = False
        try:
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass
        try:
            if self._pa:
                self._pa.terminate()
        except Exception:
            pass

async def handle_index(request):
    return web.FileResponse('index.html')

async def handle_offer(request):
    try:
        params = await request.json()
        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

        pc = RTCPeerConnection()
        video_track = ScreenCaptureTrack()
        pc.addTrack(video_track)

        # Add system audio track
        try:
            audio_track = SystemAudioTrack()
            pc.addTrack(audio_track)
        except RuntimeError as e:
            print(f"Warning: Audio disabled - {e}")
            audio_track = None

        # FIX: Explicitly loop through the active transceivers to prioritize VP8 profiles.
        # This prevents the computer from choosing broken H.264 parameters and keeps video rendering working.
        for transceiver in pc.getTransceivers():
            if transceiver.kind == "video":
                transceiver.sender.contentHint = "detail"  # Prioritize text sharpness universally
                capabilities = RTCRtpSender.getCapabilities("video")
                vp8_codecs = [c for c in capabilities.codecs if c.name == "VP8"]
                if vp8_codecs:
                    transceiver.setCodecPreferences(vp8_codecs)
            elif transceiver.kind == "audio":
                capabilities = RTCRtpSender.getCapabilities("audio")
                opus_codecs = [c for c in capabilities.codecs if c.name == "opus"]
                if opus_codecs:
                    transceiver.setCodecPreferences(opus_codecs)

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
    except Exception as e:
        traceback.print_exc()
        return web.Response(status=500, text=str(e))

if __name__ == "__main__":
    # Catch unhandled async exceptions so they don't silently kill the server
    def handle_async_exception(loop, context):
        msg = context.get('message', 'Unhandled async exception')
        exc = context.get('exception')
        print(f"\n[ASYNC ERROR] {msg}")
        if exc:
            traceback.print_exception(type(exc), exc, exc.__traceback__)

    async def install_exception_handler(app):
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(handle_async_exception)

    app = web.Application()
    app.on_startup.append(install_exception_handler)
    app.router.add_get("/", handle_index)
    app.router.add_post("/offer", handle_offer)
    web.run_app(app, host="0.0.0.0", port=8080)
