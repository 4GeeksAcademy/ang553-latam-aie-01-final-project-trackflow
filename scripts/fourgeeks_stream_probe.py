"""Opt-in, single-request QA probe for the FourGeeks streaming boundary."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import logging
import os

import httpx

from services.rag.config import DEFAULT_BASE_URL, DEFAULT_GENERATION_MODEL


@dataclass
class Observation:
    status: str = "terminal_before_cancellation"
    http_status: int | None = None
    content_deltas: int = 0
    reads_with_content: int = 0
    terminal_seen: bool = False
    task_cancelled: bool = False
    response_closed: bool = False
    deltas_after_cancel_request: int | None = None


def _frame(data: list[bytes]) -> tuple[int, bool]:
    if not data:
        return 0, False
    payload = b"\n".join(data)
    if payload == b"[DONE]":
        return 0, True
    frame = json.loads(payload)
    if not isinstance(frame, dict) or "error" in frame:
        raise ValueError("invalid stream event")
    if not isinstance(frame.get("choices"), list):
        raise ValueError("invalid choices")
    count = 0
    terminal = False
    for choice in frame["choices"]:
        delta = choice.get("delta", {}).get("content")
        count += int(isinstance(delta, str) and bool(delta))
        terminal |= choice.get("finish_reason") is not None
    return count, terminal


async def probe(client: httpx.AsyncClient, endpoint: str, key: str, model: str) -> Observation:
    result = Observation()
    cancellable = asyncio.Event()
    response: httpx.Response | None = None
    read_pending = False

    async def receive() -> None:
        nonlocal response, read_pending
        pending = b""
        fields: list[bytes] = []
        consumed = 0
        async with client.stream(
            "POST",
            endpoint,
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "text/event-stream",
                "Accept-Encoding": "identity",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Name twelve common fruits, one per line."}],
                "stream": True,
                "max_tokens": 96,
            },
        ) as opened:
            response = opened
            result.http_status = opened.status_code
            if opened.status_code != 200:
                result.status = "http_error"
                return
            if opened.headers.get("content-type", "").split(";")[0].strip().lower() != "text/event-stream":
                result.status = "not_event_stream"
                return
            batches = opened.aiter_raw()
            while True:
                read_pending = True
                try:
                    batch = await anext(batches)
                except StopAsyncIteration:
                    result.status = "eof_without_terminal"
                    return
                finally:
                    read_pending = False
                consumed += len(batch)
                if consumed > 262_144:
                    result.status = "stream_limit_exceeded"
                    return
                before = result.content_deltas
                *lines, pending = (pending + batch).split(b"\n")
                for raw in lines:
                    line = raw.removesuffix(b"\r")
                    if line.startswith(b"data:"):
                        fields.append(line[5:].removeprefix(b" "))
                    elif not line:
                        count, terminal = _frame(fields)
                        fields.clear()
                        result.content_deltas += count
                        if terminal:
                            result.terminal_seen = True
                            break
                if result.content_deltas > before:
                    result.reads_with_content += 1
                if result.terminal_seen:
                    return
                if result.reads_with_content >= 2:
                    cancellable.set()

    producer = asyncio.create_task(receive())
    ready = asyncio.create_task(cancellable.wait())
    try:
        async with asyncio.timeout(25):
            await asyncio.wait({producer, ready}, return_when=asyncio.FIRST_COMPLETED)
            if (
                ready.done()
                and not producer.done()
                and read_pending
                and response is not None
                and not response.is_closed
                and not result.terminal_seen
            ):
                before_cancel = result.content_deltas
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
                result.task_cancelled = producer.cancelled()
                result.deltas_after_cancel_request = result.content_deltas - before_cancel
                result.response_closed = response is not None and response.is_closed
                if result.task_cancelled and result.response_closed and not result.deltas_after_cancel_request:
                    result.status = "local_cancellation_observed"
                else:
                    result.status = "cancellation_not_confirmed"
            else:
                await producer
    except (TimeoutError, httpx.TimeoutException):
        result.status = "timeout"
    except httpx.HTTPError:
        result.status = "transport_error"
    except (ValueError, TypeError, AttributeError, UnicodeError):
        result.status = "invalid_event_stream"
    finally:
        for task in (producer, ready):
            if not task.done():
                task.cancel()
        await asyncio.gather(producer, ready, return_exceptions=True)
        result.response_closed = response is not None and response.is_closed
    return result


async def _live(endpoint: str, key: str, model: str) -> Observation:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15), follow_redirects=False, trust_env=False
    ) as client:
        return await probe(client, endpoint, key, model)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Allow one live QA request")
    args = parser.parse_args()
    key = os.getenv("FOUR_GEEKS_API_KEY", "").strip()
    if not key or not args.execute:
        print(json.dumps({"status": "missing_credential" if not key else "ready", "requests": 0}))
        return 2 if not key else 0
    model = os.getenv("RAG_GENERATION_MODEL", DEFAULT_GENERATION_MODEL).strip()
    try:
        base = httpx.URL(os.getenv("FOUR_GEEKS_BASE_URL", DEFAULT_BASE_URL).rstrip("/"))
    except httpx.InvalidURL:
        base = httpx.URL("")
    if base.scheme != "https" or not base.host or base.userinfo or base.query or base.fragment or not model:
        print(json.dumps({"status": "invalid_configuration", "requests": 0}))
        return 2
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    observation = asyncio.run(_live(str(base) + "/chat/completions", key, model))
    print(json.dumps(vars(observation)))
    return 0 if observation.status == "local_cancellation_observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
