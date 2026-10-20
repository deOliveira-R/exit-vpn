import logging
import os
import sys

import uvicorn

from .api import create_app


def main() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="exit-vpn[prober]: %(levelname)s %(message)s")
    uvicorn.run(create_app(), host=os.environ.get("PROBER_BIND", "0.0.0.0"),
                port=int(os.environ.get("PROBER_PORT", "9000")),
                access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
