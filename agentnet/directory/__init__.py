"""Directory (ADS) access: the client the chat app searches with, its backends, the
publisher the registration service uses, and card <-> record conversion.

DirectoryUnavailable lives here so every module raises the same class; callers
catch it by class (client, backends and publisher all re-export it)."""


class DirectoryUnavailable(RuntimeError):
    """Raised when the directory is needed but unavailable or unconfigured."""

    # Set by DirectoryPublisher.publish when the failure came after the record
    # was stored: the directory may still route it, so it is unpublished by this CID.
    cid: str | None = None
