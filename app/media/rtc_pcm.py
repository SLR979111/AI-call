"""Optional direct WebRTC data channel for the browser's existing PCM protocol."""
from __future__ import annotations

import asyncio
import json

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription


class RtcPcm:
    def __init__(self, inbox: asyncio.Queue) -> None:
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.channel = None

        @self.peer.on("datachannel")
        def on_channel(channel):
            if self.channel is not None or channel.label != "voice-pcm" or not channel.ordered:
                channel.close()
                return
            self.channel = channel

            @channel.on("message")
            def on_message(message):
                event = {"type": "websocket.receive", "rtc_pcm": True}
                if isinstance(message, bytes) and len(message) == 656:
                    event["bytes"] = message
                elif isinstance(message, str) and len(message) <= 16384:
                    event["text"] = message
                else:
                    channel.close()
                    return
                try:
                    inbox.put_nowait(event)
                except asyncio.QueueFull:
                    channel.close()

    async def answer(self, sdp: str) -> str:
        await self.peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        await self.peer.setLocalDescription(await self.peer.createAnswer())
        return self.peer.localDescription.sdp

    @property
    def ready(self) -> bool:
        return self.channel is not None and self.channel.readyState == "open"

    async def send(self, data: dict, cancel=None) -> None:
        encoded = json.dumps(data, ensure_ascii=True)
        for offset in range(0, len(encoded), 12000):
            if not self.ready:
                raise RuntimeError("WebRTC PCM channel is closed")
            async with asyncio.timeout(2):
                while self.channel.bufferedAmount > 384000:
                    if cancel:
                        cancel.check()
                    if not self.ready:
                        raise RuntimeError("WebRTC PCM channel is closed")
                    await asyncio.sleep(.01)
            if cancel:
                cancel.check()
            end = offset + 12000
            # Small ordered fragments keep large TTS PCM payloads below the SCTP message limit.
            if offset == 0:
                marker = "=" if end >= len(encoded) else "["
            else:
                marker = "!" if end >= len(encoded) else "."
            self.channel.send(marker + encoded[offset:end])
