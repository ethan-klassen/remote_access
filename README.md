remote_access

Lightweight personal remote desktop using WebRTC (Python host + browser client).

This project provides a simple WebRTC-based remote-control server that streams the host desktop (video + optional system audio) and accepts input over a data channel from a browser client. It is designed for personal use (one host and one client) and works well over a Tailscale tailnet to avoid TURN servers.


# Daily Usage (for users)

## Quickstart
### For LOCAL (within your own home internet) usage
1. Install VB-Audio Virtual Cable Drivers on your Windows PC
2. Press `Win + R` and type `mmsys.cpl`. Set the VB-Cable as your input in Playback. Then in the "Recording" tab select the CABLE Output and under "Listen" check the box that says `Listen to this device`. Set the playback through this device to your regular headhpones/speakers. Test to make sure audio still works.
3. Open your PC's command line interface by clicking the Windows key and typing `cmd` in the Windows search bar.
4. Type `ipconfig` and find the line that looks like:
`IPv4 Address. . . . . . . . . . . : X.X.X.XXX` where the X's are numbers. This is your PC's IP address on the local network. Leave this open or remember it.
5. Download the `host.exe` file from this github page under the `Releases` tab and run it. If a Windows warning asks if you want to allow this application network permission, grant it.
6. Click the big blue "Start" button.
7. On your mobile device open an internet browser such as Chrome or Safari and in the address bar at the top type your PC's IP address followed by a colon and the port number displayed in the address in the app on your PC (by default 8080). The full address bar should look something like this: `XX.X.X.XXX:8080`
8. You should now see a black screen with some buttons on the outside edges. Wiggle your finger on the screen back and forth a few times in the middle and wait a moment for the connection to load. This may take upwards of 10 seconds. Your PC's screen should come alive on your device and you should have full control of it!

### For WAN (outside your local network) usage
1. Install VB-Audio Virtual Cable Drivers on your Windows PC
2. Press `Win + R` and type `mmsys.cpl`. Set the VB-Cable as your input in Playback. Then in the "Recording" tab select the CABLE Output and under "Listen" check the box that says `Listen to this device`. Set the playback through this device to your regular headhpones/speakers. Test to make sure audio still works.
3. Install Tailscale on both your PC and desired mobile device and sign in with the same account (or invite the second account to your Tailnet).
4. Once you get your PC setup and Tailscale running, connect to the same Tailnet on your phone and click on your desktop device's name. Look for the MagicDNS name, it should be in the format `[device-name].[your-tailnet].ts.net`. Copy this address.
5. Download the `host.exe` file from this github page under the `Releases` tab and run it. If a Windows warning asks if you want to allow this application network permission, grant it.
6. Click the big blue "Start" button.
7. On your mobile device open an internet browser such as Chrome or Safari and in the address bar at the top type paste the copied MagicDNS from Tailscale followed by a colon and the port number displayed in the address in the app on your PC (by default 8080). The full address bar should look something like this: `[device-name].[your-tailnet].ts.net:8080`
8. You should now see a black screen with some buttons on the outside edges. Wiggle your finger on the screen back and forth a few times in the middle and wait a moment for the connection to load. This may take upwards of 10 seconds. Your PC's screen should come alive on your device and you should have full control of it!
*Note that you can use the application through Tailscale locally as well, though it serves no benefit over the previously mentioned LAN steps* 



# For Devs / py users

Contents

- `host.py` — main host server (aiohttp + aiortc) that captures the screen, exposes a simple signaling endpoint (`/offer`) and accepts inputs.
- `index.html` — browser client UI and WebRTC signaling/offer logic.
- `audiotest/`, `videotest/` — experimental variants and helpers.

Quick summary

- For LAN use: run `host.py` on the machine you want to control and open the page from another device on the same network.
- For WAN use without TURN: install Tailscale on both devices (recommended). Use the host's Tailscale IP or MagicDNS hostname to connect and keep the connection private and direct.

Prerequisites

- Python 3.10+ (Windows tested here)
- System libraries for PyAV/`av` (FFmpeg/libav). On Windows, installing the `av` wheel via pip often pulls the binaries; if you encounter issues, install FFmpeg separately.

Python dependencies (install into a virtualenv):

```bash
pip install aiohttp aiortc av mss pydirectinput pynput
```

If you use VB-Audio cable for system audio capture, configure that on the host and ensure the `MediaPlayer` device name in `host.py` matches.

Run the host

```bash
python host.py
```

By default the server listens on `0.0.0.0:8080` and serves `index.html`. Open the client browser and visit `http://<HOST>:8080/` (replace `<HOST>` with the host IP or hostname).

Using Tailscale (recommended for no-TURN WAN use)

1. Install Tailscale on both machines and sign into the same Tailnet: https://tailscale.com/download
2. On the host machine, run `python host.py` as above.
3. On the client machine, open the host page using the host's Tailscale IP or MagicDNS name, e.g. `http://HOSTNAME.tailnet.ts.net:8080/` or `http://100.x.y.z:8080/`.
4. The browser will POST the WebRTC offer to the host's `/offer` endpoint (same-origin). Because Tailscale provides a direct IP route, WebRTC can typically establish a peer-to-peer connection without TURN.

Optional: explicitly configure the offer URL in `index.html` if you need to load the page from a different origin. Replace the fetch call:

```js
// default in repo
const response = await fetch("/offer", { ... })

// explicit Tailscale host example
const response = await fetch("http://HOSTNAME.tailnet.ts.net:8080/offer", { ... })
```

Notes about ICE/STUN/TURN

- The code in `index.html` currently creates the `RTCPeerConnection` with an empty `iceServers` array. When using Tailscale, that is usually fine because the tailnet provides direct routing between the machines. If you later need STUN only, add a public STUN server to the `iceServers` list (STUN does not relay media and has negligible bandwidth cost).
- Avoiding TURN means you must ensure the network path exists (Tailscale, VPN, or port-forwarding + open NAT). If the host is behind CGNAT or the client network blocks peer connections, a TURN server would otherwise be required.

Long sessions and reliability

- Keep the host machine from sleeping while you use the software (set power settings accordingly).
- For hour-long daily use, add reconnect logic on the client to recreate the peer connection if it drops (the current client is minimal and will need a browser reload to reconnect).

Security

- This project offers no authentication by default — anyone who can reach the host's HTTP endpoint can initiate a session. When using on a public or shared network, restrict access via firewall rules, Tailscale ACLs, or add an auth layer to the signaling endpoint.

Troubleshooting

- No video/audio: check that `host.py` ran without errors and that the system has the required capture devices. Check terminal logs for aiortc errors.
- Client can't reach `/offer`: verify you used the host's reachable IP (LAN or Tailscale) and that Windows firewall allows inbound connections on `8080` for Python.
- Cursor or input not working: ensure `pydirectinput` or `pyautogui` (used in variants) are installed and that the host process has permissions to inject input. On Windows, UAC or other protections may block input injection for certain applications.

Free to use under license -- Developed by Ethan Klassen