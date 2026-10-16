"""Production entry point: ``python -m speedmon.serve`` (systemd)."""
import logging

import uvicorn

from .config import load_settings
from .web import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = load_settings()
    app = create_app(settings)
    # Loopback only: reachable through nginx at /speedtest/, and by the agent on this host.
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1, access_log=False, proxy_headers=False,
                server_header=False, log_config=None, timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
