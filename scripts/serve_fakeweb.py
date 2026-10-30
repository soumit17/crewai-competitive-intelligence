"""Serve data/fakeweb on 127.0.0.1:8765 until Ctrl+C (spec §4.5).

uv run python scripts/serve_fakeweb.py            # then: curl http://127.0.0.1:8765/week-2/stocksathi/pricing.html
uv run python scripts/serve_fakeweb.py --verbose  # log each request (handy to watch the scouts in M2)
"""

import sys
from urllib.parse import urlparse

from compintel.config import get_settings
from compintel.world.fakeweb import make_server

if __name__ == "__main__":
    url = urlparse(get_settings().fakeweb_url)
    server = make_server(
        get_settings().p3_data_dir / "fakeweb",
        url.hostname or "127.0.0.1",
        url.port or 8765,
        verbose="--verbose" in sys.argv,
    )
    print(f"serving data/fakeweb on {get_settings().fakeweb_url} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
