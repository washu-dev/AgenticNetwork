"""Run an ASGI app on a chosen host with INFO logging (used for the registration service).

``registration_service.server``'s own entry point always binds ``0.0.0.0``; the dev
stack keeps every service on ``127.0.0.1`` because the service trusts identity
headers. This launcher runs under the service's own venv (it imports uvicorn from
there) and configures logging the same way the service's ``__main__`` does.

Usage: ``python -m devstack.serve_asgi registration_service.server:app --host 127.0.0.1 --port 8091``
"""

from __future__ import annotations

import argparse
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app", help="ASGI app import path, module:attribute")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, os.getenv("DEVSTACK_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    import uvicorn  # from the service's venv

    uvicorn.run(args.app, host=args.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
