import gzip
import io
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_DECOMPRESSED_BYTES = 10 * 1024 * 1024  # 10 MB


class GzipRequestMiddleware:
    """ASGI Middleware to transparently decompress Content-Encoding: gzip requests up to 10MB."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_DECOMPRESSED_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_encoding = headers.get("content-encoding", "").lower().strip()
        if "gzip" not in content_encoding:
            await self.app(scope, receive, send)
            return

        # Read full compressed body
        compressed_chunks: list[bytes] = []
        total_compressed = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if chunk:
                total_compressed += len(chunk)
                if total_compressed > self.max_bytes:
                    resp = JSONResponse(
                        {"detail": "Compressed payload exceeds 10MB limit"},
                        status_code=413,
                    )
                    await resp(scope, receive, send)
                    return
                compressed_chunks.append(chunk)
            more_body = message.get("more_body", False)

        compressed_data = b"".join(compressed_chunks)
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(compressed_data), mode="rb") as gz:
                decompressed = gz.read(self.max_bytes + 1)
        except Exception as exc:
            resp = JSONResponse(
                {"detail": f"Invalid gzip payload: {exc}"},
                status_code=400,
            )
            await resp(scope, receive, send)
            return

        if len(decompressed) > self.max_bytes:
            resp = JSONResponse(
                {"detail": "Decompressed payload exceeds 10MB limit"},
                status_code=413,
            )
            await resp(scope, receive, send)
            return

        mutable_headers = MutableHeaders(scope=scope)
        del mutable_headers["content-encoding"]
        mutable_headers["content-length"] = str(len(decompressed))

        body_sent = False

        async def decompressed_receive() -> Message:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {
                    "type": "http.request",
                    "body": decompressed,
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, decompressed_receive, send)
