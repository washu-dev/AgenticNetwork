from agntcy_app_sdk.semantic.a2a.client.config import ClientConfig
from agntcy_app_sdk.semantic.a2a.client.factory import A2AClientFactory


def make_a2a_client_factory() -> A2AClientFactory:
    """Build an A2AClientFactory configured for JSONRPC over HTTP.

    The lab's network uses JSONRPC (Email Agent / Coding Agent). The default
    ClientConfig already supports JSONRPC via the upstream a2a-sdk's built-in
    httpx-based transport — no extra config needed for outbound calls.
    """
    return A2AClientFactory(ClientConfig())
