from collections.abc import Callable
from typing import Any

from app.nova.events import DEFAULT_NOVA_MODEL_ID, NovaParsedEvent, event_to_bytes, parse_nova_event_bytes


class NovaClientError(RuntimeError):
    pass


class NovaClient:
    def __init__(
        self,
        *,
        model_id: str = DEFAULT_NOVA_MODEL_ID,
        region: str = "us-east-1",
        bedrock_client_factory: Callable[[], Any] | None = None,
        stream_input_factory: Callable[[str], Any] | None = None,
        input_chunk_factory: Callable[[bytes], Any] | None = None,
    ) -> None:
        self.model_id = model_id
        self.region = region
        self._bedrock_client_factory = bedrock_client_factory
        self._stream_input_factory = stream_input_factory
        self._input_chunk_factory = input_chunk_factory
        self._bedrock_client: Any | None = None
        self._stream: Any | None = None
        self._output_receiver: Any | None = None

    async def open(self) -> None:
        if self._stream is not None:
            return
        self._bedrock_client = self._bedrock_client_factory() if self._bedrock_client_factory else self._create_bedrock_client()
        operation_input = (
            self._stream_input_factory(self.model_id)
            if self._stream_input_factory
            else _default_stream_input(self.model_id)
        )
        self._stream = await self._bedrock_client.invoke_model_with_bidirectional_stream(
            operation_input
        )

    async def send_event(self, event: dict[str, Any]) -> None:
        if self._stream is None:
            raise NovaClientError("Nova stream is not open")
        event_bytes = event_to_bytes(event)
        chunk = self._input_chunk_factory(event_bytes) if self._input_chunk_factory else _default_input_chunk(event_bytes)
        await self._stream.input_stream.send(chunk)

    async def receive_event(self) -> NovaParsedEvent:
        if self._stream is None:
            raise NovaClientError("Nova stream is not open")
        if self._output_receiver is None:
            output = await self._stream.await_output()
            self._output_receiver = output[1]
        result = await self._output_receiver.receive()
        if not getattr(result, "value", None) or not getattr(result.value, "bytes_", None):
            raise NovaClientError("Nova stream returned an empty output payload")
        return parse_nova_event_bytes(result.value.bytes_)

    async def close(self) -> None:
        if self._stream is None:
            return
        await self._stream.input_stream.close()
        self._stream = None
        self._output_receiver = None

    def _create_bedrock_client(self) -> Any:
        sdk_runtime = _import_bedrock_runtime()
        credentials = _resolve_aws_credentials()
        config = sdk_runtime.Config(
            endpoint_uri=f"https://bedrock-runtime.{self.region}.amazonaws.com",
            region=self.region,
            aws_access_key_id=credentials.access_key,
            aws_secret_access_key=credentials.secret_key,
            aws_session_token=credentials.token,
            aws_credentials_identity_resolver=sdk_runtime.StaticCredentialsResolver(),
            auth_scheme_resolver=sdk_runtime.HTTPAuthSchemeResolver(),
            auth_schemes={"aws.auth#sigv4": sdk_runtime.SigV4AuthScheme(service="bedrock")},
        )
        return sdk_runtime.BedrockRuntimeClient(config=config)


def _import_bedrock_runtime() -> Any:
    try:
        import boto3
        from aws_sdk_bedrock_runtime.client import BedrockRuntimeClient
        from aws_sdk_bedrock_runtime.config import Config, HTTPAuthSchemeResolver
        from smithy_aws_core.auth.sigv4 import SigV4AuthScheme
        from smithy_aws_core.identity import StaticCredentialsResolver
    except ModuleNotFoundError as exc:
        raise NovaClientError(
            "boto3, aws-sdk-bedrock-runtime, and smithy-aws-core are required for real Nova streams. "
            "Install dependencies with `python -m pip install -r requirements.txt`."
        ) from exc

    class RuntimeImports:
        pass

    imports = RuntimeImports()
    imports.BedrockRuntimeClient = BedrockRuntimeClient
    imports.Config = Config
    imports.HTTPAuthSchemeResolver = HTTPAuthSchemeResolver
    imports.SigV4AuthScheme = SigV4AuthScheme
    imports.StaticCredentialsResolver = StaticCredentialsResolver
    imports.boto3 = boto3
    return imports


def _import_bedrock_models() -> Any:
    try:
        from aws_sdk_bedrock_runtime.models import (
            BidirectionalInputPayloadPart,
            InvokeModelWithBidirectionalStreamInputChunk,
            InvokeModelWithBidirectionalStreamOperationInput,
        )
    except ModuleNotFoundError as exc:
        raise NovaClientError(
            "aws-sdk-bedrock-runtime is required for Nova stream event chunks. "
            "Install dependencies with `python -m pip install -r requirements.txt`."
        ) from exc

    class ModelImports:
        pass

    imports = ModelImports()
    imports.BidirectionalInputPayloadPart = BidirectionalInputPayloadPart
    imports.InvokeModelWithBidirectionalStreamInputChunk = InvokeModelWithBidirectionalStreamInputChunk
    imports.InvokeModelWithBidirectionalStreamOperationInput = InvokeModelWithBidirectionalStreamOperationInput
    return imports


def _default_stream_input(model_id: str) -> Any:
    sdk_models = _import_bedrock_models()
    return sdk_models.InvokeModelWithBidirectionalStreamOperationInput(model_id=model_id)


def _default_input_chunk(event_bytes: bytes) -> Any:
    sdk_models = _import_bedrock_models()
    return sdk_models.InvokeModelWithBidirectionalStreamInputChunk(
        value=sdk_models.BidirectionalInputPayloadPart(bytes_=event_bytes)
    )


def _resolve_aws_credentials() -> Any:
    sdk_runtime = _import_bedrock_runtime()
    credentials = sdk_runtime.boto3.Session().get_credentials()
    if credentials is None:
        raise NovaClientError(
            "AWS credentials were not found. Configure AWS_PROFILE or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY before running the Nova spike."
        )
    return credentials.get_frozen_credentials()
