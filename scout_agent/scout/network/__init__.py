import logging


class _SuppressSessionNotFound(logging.Filter):
    """Drop a noisy SDK warning that fires on idempotent SLIM session cleanup.

    `agntcy_app_sdk.transport.slim.session_manager._local_cache_cleanup` logs
    a WARNING when it tries to remove a session that the underlying Rust SLIM
    channel already cleaned up. The cleanup is idempotent — the warning is
    harmless. We filter the specific message at the logger level so it doesn't
    appear in `scout serve -v` output or `call_scout.py` output.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "cannot be removed from local cache" not in record.getMessage()


logging.getLogger("agntcy_app_sdk.transport.slim.session_manager").addFilter(
    _SuppressSessionNotFound()
)
