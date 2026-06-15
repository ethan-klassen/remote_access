import os
import json
import asyncio
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer

# Standard VB-Cable configuration
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
        if pc.connectionState in ["failed", "closed"]:
            await pc.close()

    # Fixed: Restored to standard single argument syntax
    if player.audio:
        pc.addTrack(player.audio) 

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    # Fully complete the ICE routing table mapping before returning
    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.05)

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
    web.run_app(app, host="0.0.0.0", port=8080)
