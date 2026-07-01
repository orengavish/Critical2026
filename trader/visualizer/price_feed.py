"""
visualizer/price_feed.py
Background thread that polls IB LIVE for current MES price every N seconds.
Stores latest price in memory for the Flask routes to read.
Uses dedicated visualizer_client_ids from config (falls back to live_client_ids).
"""

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_price: float | None = None
_price_time: datetime | None = None
_symbol: str = "MES"
_lock = threading.Lock()
_thread: threading.Thread | None = None
_ibc = None


def get_latest() -> tuple[float | None, datetime | None]:
    with _lock:
        return _price, _price_time


def _poll_loop(cfg, symbol: str, interval: int):
    global _price, _price_time, _ibc
    import sys
    import asyncio
    _HERE = Path(__file__).parent.parent.parent   # trader/visualizer -> trader -> galgo2026
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    # ib_insync (via eventkit) calls asyncio.get_event_loop() at import time;
    # background threads have no event loop by default in Python 3.10+.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    from lib.ib_client import IBClient
    from lib.logger import get_logger
    log = get_logger("price_feed")

    ibc = IBClient(cfg)
    _ibc = ibc

    while True:
        try:
            if not ibc.is_live_connected():
                ibc.connect(live=True, paper=False)
            price = ibc.get_price(symbol)
            if price and price == price:  # not nan
                with _lock:
                    _price = price
                    _price_time = datetime.now(timezone.utc)
        except Exception as e:
            log.debug(f"Price poll error: {e}")
        time.sleep(interval)


def start(cfg, symbol: str = "MES", interval: int = 5):
    global _thread, _symbol
    _symbol = symbol
    _thread = threading.Thread(
        target=_poll_loop, args=(cfg, symbol, interval), daemon=True
    )
    _thread.start()


def stop():
    global _ibc
    if _ibc:
        try:
            _ibc.disconnect()
        except Exception:
            pass
        _ibc = None
