import os
import json
import asyncio
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer

# Pass the exact string for the VB-Cable driver
player = MediaPlayer(
    "audio=CABLE Output (VB-Audio Virtual Cable)",
    format="dshow",
    options={"audio_buffer_size": "20"}
)



async def index(request):
    content = open(os.path.join(os.path.dirname(__file__), "index.html"), "r").read()
    return web.Response(content_type="text/html", text=content)

async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    
    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        if pc.connectionState == "failed":
            await pc.close()

    # Read audio from system loopback device and attach to WebRTC stream
    if player.audio:
        pc.addTrack(player.audio)

    # WebRTC Handshake
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.Response(
        content_type="application/json",
        text=json.dumps({
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type
        })
    )

app = web.Application()
app.router.add_get("/", index)
app.router.add_post("/offer", offer)

if __name__ == "__main__":
    # Host on 0.0.0.0 so your mobile phone can access it over local Wi-Fi
    web.run_app(app, host="0.0.0.0", port=8080)
