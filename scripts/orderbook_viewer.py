from __future__ import annotations

import asyncio
import queue
import sys
import threading
import tkinter as tk
from contextlib import suppress
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from websockets.exceptions import InvalidStatus

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshi_mm_bot.api.auth import KalshiAuth
from kalshi_mm_bot.api.feed_controller import FeedController, ORDERBOOK_CHANNEL
from kalshi_mm_bot.api.rest import KalshiRestClient
from kalshi_mm_bot.api.websocket import KalshiWebSocketClient
from kalshi_mm_bot.config import load_settings
from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import format_count_fp, format_price_fp

Row = tuple[str, str, str, str, str]
Event = tuple[str, Any]


class OrderbookViewer(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Kalshi Orderbooks")
        self.geometry("900x520")
        self.minsize(760, 420)

        self._events: queue.Queue[Event] = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None

        self._tickers_var = tk.StringVar()
        self._prod_var = tk.BooleanVar(value=True)
        self._refresh_var = tk.StringVar(value="0.25")
        self._status_var = tk.StringVar(value="Idle")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._poll_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        controls = ttk.Frame(outer)
        controls.pack(fill=tk.X)

        ttk.Label(controls, text="Tickers").grid(row=0, column=0, sticky="w")
        ticker_entry = ttk.Entry(controls, textvariable=self._tickers_var)
        ticker_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ticker_entry.focus_set()

        ttk.Label(controls, text="Refresh sec").grid(row=0, column=1, sticky="w")
        ttk.Entry(controls, textvariable=self._refresh_var, width=10).grid(
            row=1,
            column=1,
            sticky="w",
            padx=(0, 8),
        )

        ttk.Checkbutton(controls, text="Production", variable=self._prod_var).grid(
            row=1,
            column=2,
            sticky="w",
            padx=(0, 8),
        )

        self._start_button = ttk.Button(controls, text="Start", command=self._start)
        self._start_button.grid(row=1, column=3, sticky="e", padx=(0, 6))
        self._stop_button = ttk.Button(controls, text="Stop", command=self._stop, state=tk.DISABLED)
        self._stop_button.grid(row=1, column=4, sticky="e")

        controls.columnconfigure(0, weight=1)

        columns = ("ticker", "bid", "bid_size", "ask", "ask_size")
        self._table = ttk.Treeview(outer, columns=columns, show="headings", height=16)
        self._table.pack(fill=tk.BOTH, expand=True, pady=(12, 8))

        headings = {
            "ticker": "Ticker",
            "bid": "Best Bid",
            "bid_size": "Bid Size",
            "ask": "Best Ask",
            "ask_size": "Ask Size",
        }
        widths = {
            "ticker": 320,
            "bid": 120,
            "bid_size": 110,
            "ask": 120,
            "ask_size": 110,
        }

        for column in columns:
            self._table.heading(column, text=headings[column])
            self._table.column(column, width=widths[column], anchor=tk.E)

        self._table.column("ticker", anchor=tk.W)

        ttk.Label(outer, textvariable=self._status_var, anchor="w").pack(fill=tk.X)

    def _start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return

        tickers = tuple(dict.fromkeys(self._tickers_var.get().split()))
        if not tickers:
            messagebox.showerror("Missing tickers", "Enter one or more market tickers.")
            return

        try:
            refresh = float(self._refresh_var.get())
        except ValueError:
            messagebox.showerror("Invalid refresh", "Refresh must be a number of seconds.")
            return

        if refresh <= 0:
            messagebox.showerror("Invalid refresh", "Refresh must be greater than zero.")
            return

        self._clear_table()
        self._set_running(True)
        self._status_var.set("Connecting...")
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=_worker_main,
            args=(self._events, self._stop_event, tickers, self._prod_var.get(), refresh),
            daemon=True,
        )
        self._worker.start()

    def _stop(self) -> None:
        self._stop_event.set()
        self._status_var.set("Stopping...")
        self._stop_button.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        self._stop_event.set()
        self.destroy()

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self._events.get_nowait()

                if event == "rows":
                    self._update_rows(payload)
                elif event == "status":
                    self._status_var.set(payload)
                elif event == "error":
                    self._status_var.set("Error")
                    self._set_running(False)
                    messagebox.showerror("Kalshi orderbook viewer", payload)
                elif event == "stopped":
                    self._set_running(False)
                    self._status_var.set("Stopped")
        except queue.Empty:
            pass

        self.after(50, self._poll_events)

    def _update_rows(self, rows: tuple[Row, ...]) -> None:
        existing = set(self._table.get_children(""))

        for row in rows:
            ticker = row[0]
            if ticker in existing:
                self._table.item(ticker, values=row)
                existing.remove(ticker)
            else:
                self._table.insert("", tk.END, iid=ticker, values=row)

        for ticker in existing:
            self._table.delete(ticker)

    def _clear_table(self) -> None:
        for item in self._table.get_children(""):
            self._table.delete(item)

    def _set_running(self, running: bool) -> None:
        self._start_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self._stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)


def _worker_main(
    events: queue.Queue[Event],
    stop_event: threading.Event,
    tickers: tuple[str, ...],
    prod: bool,
    refresh: float,
) -> None:
    try:
        asyncio.run(_run_feed(events, stop_event, tickers, prod, refresh))
    except Exception as error:
        events.put(("error", str(error)))


async def _run_feed(
    events: queue.Queue[Event],
    stop_event: threading.Event,
    tickers: tuple[str, ...],
    prod: bool,
    refresh: float,
) -> None:
    settings = load_settings()
    auth = KalshiAuth(settings.api_key_id, settings.private_key_path)
    rest_url = settings.prod_rest_base_url if prod else settings.demo_rest_base_url
    ws_url = settings.prod_ws_url if prod else settings.demo_ws_url
    environment = "production" if prod else "demo"
    controller = FeedController(
        rest=KalshiRestClient(rest_url, auth),
        ws=KalshiWebSocketClient(ws_url, auth),
    )
    receiver: asyncio.Task[None] | None = None

    try:
        events.put(("status", f"Connecting to {environment}..."))
        await controller.connect()
        events.put(("status", "Subscribing..."))
        await controller.subscribe(tickers, channels=(ORDERBOOK_CHANNEL,))
        receiver = asyncio.create_task(controller.run_forever())
        events.put(("status", "Live"))

        while not stop_event.is_set():
            events.put(("rows", _snapshot_rows(controller, tickers)))
            await asyncio.sleep(refresh)
    except InvalidStatus as error:
        if getattr(error.response, "status_code", None) == 401:
            raise RuntimeError(
                f"Kalshi rejected websocket auth for {environment} (HTTP 401). "
                "Use production keys with Production checked, or demo keys with it unchecked."
            ) from error

        raise
    finally:
        if receiver is not None:
            receiver.cancel()
            with suppress(asyncio.CancelledError):
                await receiver

        await controller.close()
        events.put(("stopped", None))


def _snapshot_rows(controller: FeedController, tickers: tuple[str, ...]) -> tuple[Row, ...]:
    rows: list[Row] = []

    for ticker in tickers:
        book = controller.orderbooks.get(ticker)
        bid_price, bid_size = _best_level(book, "bid")
        ask_price, ask_size = _best_level(book, "ask")
        rows.append((ticker, bid_price, bid_size, ask_price, ask_size))

    return tuple(rows)


def _best_level(book: Orderbook | None, side: str) -> tuple[str, str]:
    if book is None:
        return "-", "-"

    if side == "bid":
        price = book.best_bid
        levels = book.bids
    else:
        price = book.best_ask
        levels = book.asks

    if price is None:
        return "-", "-"

    return format_price_fp(price), format_count_fp(levels[price])


def main() -> None:
    OrderbookViewer().mainloop()


if __name__ == "__main__":
    main()
