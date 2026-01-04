import httpx
import pytest

from takopi.logging import setup_logging
from takopi.telegram import TelegramClient, TelegramRetryAfter


@pytest.mark.anyio
async def test_telegram_429_no_retry() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            429,
            json={
                "ok": False,
                "description": "retry",
                "parameters": {"retry_after": 3},
            },
            request=request,
        )

    transport = httpx.MockTransport(handler)

    client = httpx.AsyncClient(transport=transport)
    try:
        tg = TelegramClient("123:abcDEF_ghij", http_client=client)
        with pytest.raises(TelegramRetryAfter) as exc:
            await tg._post("sendMessage", {"chat_id": 1, "text": "hi"})
    finally:
        await client.aclose()

    assert exc.value.retry_after == 3
    assert len(calls) == 1


@pytest.mark.anyio
async def test_no_token_in_logs_on_http_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "123:abcDEF_ghij"
    setup_logging(debug=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops", request=request)

    transport = httpx.MockTransport(handler)

    client = httpx.AsyncClient(transport=transport)
    try:
        tg = TelegramClient(token, http_client=client)
        await tg._post("getUpdates", {"timeout": 1})
    finally:
        await client.aclose()

    out = capsys.readouterr().out
    assert token not in out
    assert "bot[REDACTED]" in out


@pytest.mark.anyio
async def test_get_file_returns_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getFile")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"file_id": "abc", "file_path": "voice/clip.ogg"},
            },
            request=request,
        )

    transport = httpx.MockTransport(handler)

    client = httpx.AsyncClient(transport=transport)
    try:
        tg = TelegramClient("123:abcDEF_ghij", http_client=client)
        result = await tg.get_file("abc")
    finally:
        await client.aclose()

    assert result == {"file_id": "abc", "file_path": "voice/clip.ogg"}


@pytest.mark.anyio
async def test_download_file_returns_bytes() -> None:
    token = "123:abcDEF_ghij"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/file/bot{token}/voice/clip.ogg"
        return httpx.Response(200, content=b"data", request=request)

    transport = httpx.MockTransport(handler)

    client = httpx.AsyncClient(transport=transport)
    try:
        tg = TelegramClient(token, http_client=client)
        result = await tg.download_file("voice/clip.ogg")
    finally:
        await client.aclose()

    assert result == b"data"
