import asyncio

import pytest

from app.nova import NovaClient, NovaClientError, session_end_event


class FakeOperationInput:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id


class FakeChunkValue:
    def __init__(self, bytes_: bytes) -> None:
        self.bytes_ = bytes_


class FakeChunk:
    def __init__(self, bytes_: bytes) -> None:
        self.value = FakeChunkValue(bytes_)


class FakeResultValue:
    def __init__(self, bytes_: bytes | None) -> None:
        self.bytes_ = bytes_


class FakeResult:
    def __init__(self, bytes_: bytes | None) -> None:
        self.value = FakeResultValue(bytes_) if bytes_ is not None else None


class FakeReceiver:
    def __init__(self, bytes_: bytes | None) -> None:
        self.bytes_ = bytes_

    async def receive(self) -> FakeResult:
        return FakeResult(self.bytes_)


class FakeInputStream:
    def __init__(self) -> None:
        self.sent = []
        self.closed = False

    async def send(self, chunk) -> None:
        self.sent.append(chunk)

    async def close(self) -> None:
        self.closed = True


class FakeStream:
    def __init__(self, output_bytes: bytes | None = b'{"event":{"sessionEnd":{}}}') -> None:
        self.input_stream = FakeInputStream()
        self.output_bytes = output_bytes
        self.await_output_calls = 0

    async def await_output(self):
        self.await_output_calls += 1
        return (None, FakeReceiver(self.output_bytes))


class FakeBedrockClient:
    def __init__(self, stream: FakeStream) -> None:
        self.stream = stream
        self.model_ids = []

    async def invoke_model_with_bidirectional_stream(self, operation_input):
        self.model_ids.append(operation_input.model_id)
        return self.stream


def make_client(bedrock_client: FakeBedrockClient | None = None, stream: FakeStream | None = None) -> NovaClient:
    if bedrock_client is None:
        bedrock_client = FakeBedrockClient(stream or FakeStream())
    return NovaClient(
        model_id="amazon.nova-2-sonic-v1:0",
        region="us-east-1",
        bedrock_client_factory=lambda: bedrock_client,
        stream_input_factory=FakeOperationInput,
        input_chunk_factory=FakeChunk,
    )


def test_nova_client_open_send_receive_close_with_fake_stream() -> None:
    async def run() -> None:
        stream = FakeStream()
        bedrock_client = FakeBedrockClient(stream)
        client = make_client(bedrock_client)

        await client.open()
        await client.send_event(session_end_event())
        event = await client.receive_event()
        await client.close()

        assert bedrock_client.model_ids == ["amazon.nova-2-sonic-v1:0"]
        assert len(stream.input_stream.sent) == 1
        assert stream.input_stream.sent[0].value.bytes_ == b'{"event":{"sessionEnd":{}}}'
        assert event.event_type == "session_end"
        assert stream.input_stream.closed is True

    asyncio.run(run())


def test_nova_client_rejects_send_before_open() -> None:
    async def run() -> None:
        client = NovaClient()

        with pytest.raises(NovaClientError):
            await client.send_event(session_end_event())

    asyncio.run(run())


def test_nova_client_rejects_empty_output_payload() -> None:
    async def run() -> None:
        stream = FakeStream(output_bytes=None)
        bedrock_client = FakeBedrockClient(stream)
        client = make_client(bedrock_client)

        await client.open()

        with pytest.raises(NovaClientError):
            await client.receive_event()

    asyncio.run(run())


def test_nova_client_reuses_output_receiver_for_stream_lifetime() -> None:
    async def run() -> None:
        stream = FakeStream()
        bedrock_client = FakeBedrockClient(stream)
        client = make_client(bedrock_client)

        await client.open()
        await client.receive_event()
        await client.receive_event()
        await client.close()

        assert stream.await_output_calls == 1

    asyncio.run(run())
