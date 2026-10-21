"""Entry point: build the prober (tunnel, exit check, engines) and serve its API."""

import logging
import os
import sys

import uvicorn

from .api import create_app
from .engines import BrowserEngine, HttpEngine
from .exitcheck import ExitCheck
from .probe import Prober
from .tunnel import ProbeTunnel


def main() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="exit-vpn[prober]: %(levelname)s %(message)s")
    http, browser = HttpEngine(), BrowserEngine()
    prober = Prober(ProbeTunnel(os.environ.get("PROBE_IF", "wg-probe")), ExitCheck(http.get_text),
                    http, browser)
    uvicorn.run(create_app(prober, browser), host=os.environ.get("PROBER_BIND", "0.0.0.0"),
                port=int(os.environ.get("PROBER_PORT", "9000")),
                access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
