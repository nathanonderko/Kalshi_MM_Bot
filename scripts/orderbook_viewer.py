from __future__ import annotations

import asyncio
import queue
import sys
import threading
import time
import tkinter as tk
from collections.abc import Iterable, Iterator
from contextlib import suppress
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
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
from kalshi_mm_bot.market.price import format_price_fp, parse_count_fp
from kalshi_mm_bot.market.tickers import parse_ticker_tuple
from kalshi_mm_bot.market.view import TopOfBookRow, top_of_book_rows
from kalshi_mm_bot.recording import RecordingManifest, RecordingSessionWriter
from kalshi_mm_bot.recording.clients import RecordingWebSocketClient
from kalshi_mm_bot.recording.paths import (
    default_recording_dir,
    latest_recording_dir,
    require_recording_path,
)
from kalshi_mm_bot.sim import (
    BacktestResult,
    BacktestSummary,
    BacktestUpdate,
    backtest_summary_rows,
    fill_model_from_name,
    format_contract_count,
    run_replay_backtest,
)
from kalshi_mm_bot.strategy import STRATEGY_NAMES, strategy_from_name


Row = TopOfBookRow
Event = tuple[str, Any]
MAX_FILL_ROWS = 250


class OrderbookViewer(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Kalshi Market Workbench")
        self.geometry("1080x700")
        self.minsize(880, 560)

        self._events: queue.Queue[Event] = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None

        self._record_events: queue.Queue[Event] = queue.Queue()
        self._record_stop_event = threading.Event()
        self._record_worker: threading.Thread | None = None

        self._replay_events: queue.Queue[Event] = queue.Queue()
        self._replay_stop_event = threading.Event()
        self._replay_worker: threading.Thread | None = None
        self._replay_seen_fills: set[str] = set()

        self._tickers_var = tk.StringVar()
        self._prod_var = tk.BooleanVar(value=True)
        self._refresh_var = tk.StringVar(value="0.25")
        self._status_var = tk.StringVar(value="Idle")

        self._record_tickers_var = tk.StringVar()
        self._record_prod_var = tk.BooleanVar(value=True)
        self._record_duration_var = tk.StringVar()
        self._record_output_var = tk.StringVar()
        self._record_status_var = tk.StringVar(value="Idle")

        self._replay_recording_var = tk.StringVar()
        self._replay_strategy_var = tk.StringVar(value="adaptive")
        self._replay_fill_model_var = tk.StringVar(value="queue")
        self._replay_speed_var = tk.StringVar(value="0")
        self._replay_refresh_var = tk.StringVar(value="0.25")
        self._replay_order_size_var = tk.StringVar(value="1.00")
        self._replay_max_position_var = tk.StringVar(value="10.00")
        self._replay_latency_var = tk.StringVar(value="0")
        self._replay_status_var = tk.StringVar(value="Idle")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._poll_events)

    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True)

        self._build_live_tab(notebook)
        self._build_record_tab(notebook)
        self._build_replay_tab(notebook)

    def _build_live_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=12)
        notebook.add(tab, text="Live")

        controls = ttk.Frame(tab)
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
        self._table = ttk.Treeview(tab, columns=columns, show="headings", height=18)
        self._table.pack(fill=tk.BOTH, expand=True, pady=(12, 8))
        _configure_book_table(self._table)

        ttk.Label(tab, textvariable=self._status_var, anchor="w").pack(fill=tk.X)

    def _build_record_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=12)
        notebook.add(tab, text="Record")

        controls = ttk.Frame(tab)
        controls.pack(fill=tk.X)

        ttk.Label(controls, text="Tickers").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self._record_tickers_var).grid(
            row=1,
            column=0,
            sticky="ew",
            padx=(0, 8),
        )

        ttk.Label(controls, text="Duration sec").grid(row=0, column=1, sticky="w")
        ttk.Entry(controls, textvariable=self._record_duration_var, width=12).grid(
            row=1,
            column=1,
            sticky="w",
            padx=(0, 8),
        )

        ttk.Checkbutton(controls, text="Production", variable=self._record_prod_var).grid(
            row=1,
            column=2,
            sticky="w",
            padx=(0, 8),
        )

        self._record_start_button = ttk.Button(
            controls,
            text="Start Recording",
            command=self._start_recording,
        )
        self._record_start_button.grid(row=1, column=3, sticky="e", padx=(0, 6))
        self._record_stop_button = ttk.Button(
            controls,
            text="Stop",
            command=self._stop_recording,
            state=tk.DISABLED,
        )
        self._record_stop_button.grid(row=1, column=4, sticky="e")

        controls.columnconfigure(0, weight=1)

        output = ttk.Frame(tab)
        output.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(output, text="Output directory").pack(side=tk.LEFT)
        ttk.Entry(output, textvariable=self._record_output_var).pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
            padx=8,
        )
        ttk.Button(output, text="Browse", command=self._browse_record_output).pack(side=tk.LEFT)

        status_frame = ttk.Frame(tab)
        status_frame.pack(fill=tk.BOTH, expand=True, pady=(12, 8))
        self._record_log = tk.Text(status_frame, height=18, wrap=tk.WORD)
        self._record_log.pack(fill=tk.BOTH, expand=True)
        self._record_log.configure(state=tk.DISABLED)

        ttk.Label(tab, textvariable=self._record_status_var, anchor="w").pack(fill=tk.X)

    def _build_replay_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=12)
        notebook.add(tab, text="Replay / Backtest")

        recording_row = ttk.Frame(tab)
        recording_row.pack(fill=tk.X)
        ttk.Label(recording_row, text="Recording").pack(side=tk.LEFT)
        ttk.Entry(recording_row, textvariable=self._replay_recording_var).pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
            padx=8,
        )
        ttk.Button(recording_row, text="Browse", command=self._browse_replay_recording).pack(
            side=tk.LEFT,
            padx=(0, 6),
        )
        ttk.Button(recording_row, text="Newest", command=self._use_latest_recording).pack(side=tk.LEFT)

        controls = ttk.Frame(tab)
        controls.pack(fill=tk.X, pady=(12, 0))

        ttk.Label(controls, text="Strategy").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            controls,
            textvariable=self._replay_strategy_var,
            values=STRATEGY_NAMES,
            width=12,
            state="readonly",
        ).grid(row=1, column=0, sticky="w", padx=(0, 8))

        ttk.Label(controls, text="Fill model").grid(row=0, column=1, sticky="w")
        ttk.Combobox(
            controls,
            textvariable=self._replay_fill_model_var,
            values=("queue", "optimistic", "pessimistic"),
            width=14,
            state="readonly",
        ).grid(row=1, column=1, sticky="w", padx=(0, 8))

        ttk.Label(controls, text="Speed").grid(row=0, column=2, sticky="w")
        ttk.Entry(controls, textvariable=self._replay_speed_var, width=10).grid(
            row=1,
            column=2,
            sticky="w",
            padx=(0, 8),
        )

        ttk.Label(controls, text="Refresh sec").grid(row=0, column=3, sticky="w")
        ttk.Entry(controls, textvariable=self._replay_refresh_var, width=10).grid(
            row=1,
            column=3,
            sticky="w",
            padx=(0, 8),
        )

        ttk.Label(controls, text="Order size").grid(row=0, column=4, sticky="w")
        ttk.Entry(controls, textvariable=self._replay_order_size_var, width=10).grid(
            row=1,
            column=4,
            sticky="w",
            padx=(0, 8),
        )

        ttk.Label(controls, text="Max pos").grid(row=0, column=5, sticky="w")
        ttk.Entry(controls, textvariable=self._replay_max_position_var, width=10).grid(
            row=1,
            column=5,
            sticky="w",
            padx=(0, 8),
        )

        ttk.Label(controls, text="Latency sec").grid(row=0, column=6, sticky="w")
        ttk.Entry(controls, textvariable=self._replay_latency_var, width=10).grid(
            row=1,
            column=6,
            sticky="w",
            padx=(0, 8),
        )

        self._replay_start_button = ttk.Button(
            controls,
            text="Run Backtest",
            command=self._start_replay,
        )
        self._replay_start_button.grid(row=1, column=7, sticky="e", padx=(0, 6))
        self._replay_stop_button = ttk.Button(
            controls,
            text="Stop",
            command=self._stop_replay,
            state=tk.DISABLED,
        )
        self._replay_stop_button.grid(row=1, column=8, sticky="e")

        controls.columnconfigure(9, weight=1)

        panes = ttk.PanedWindow(tab, orient=tk.VERTICAL)
        panes.pack(fill=tk.BOTH, expand=True, pady=(12, 8))

        top_pane = ttk.Frame(panes)
        panes.add(top_pane, weight=3)

        columns = ("ticker", "bid", "bid_size", "ask", "ask_size")
        self._replay_table = ttk.Treeview(top_pane, columns=columns, show="headings", height=10)
        self._replay_table.pack(fill=tk.BOTH, expand=True)
        _configure_book_table(self._replay_table)

        bottom_pane = ttk.PanedWindow(panes, orient=tk.HORIZONTAL)
        panes.add(bottom_pane, weight=2)

        summary_frame = ttk.Frame(bottom_pane)
        bottom_pane.add(summary_frame, weight=1)
        self._summary_table = ttk.Treeview(
            summary_frame,
            columns=("metric", "value"),
            show="headings",
            height=10,
        )
        self._summary_table.pack(fill=tk.BOTH, expand=True)
        self._summary_table.heading("metric", text="Metric")
        self._summary_table.heading("value", text="Value")
        self._summary_table.column("metric", width=150, anchor=tk.W)
        self._summary_table.column("value", width=160, anchor=tk.E)

        fills_frame = ttk.Frame(bottom_pane)
        bottom_pane.add(fills_frame, weight=2)
        self._fills_table = ttk.Treeview(
            fills_frame,
            columns=("time", "ticker", "action", "price", "count", "reason"),
            show="headings",
            height=10,
        )
        self._fills_table.pack(fill=tk.BOTH, expand=True)

        fill_headings = {
            "time": "Time",
            "ticker": "Ticker",
            "action": "Action",
            "price": "Price",
            "count": "Count",
            "reason": "Reason",
        }
        fill_widths = {
            "time": 170,
            "ticker": 240,
            "action": 80,
            "price": 80,
            "count": 80,
            "reason": 150,
        }

        for column in fill_headings:
            self._fills_table.heading(column, text=fill_headings[column])
            self._fills_table.column(column, width=fill_widths[column], anchor=tk.E)

        self._fills_table.column("ticker", anchor=tk.W)
        self._fills_table.column("reason", anchor=tk.W)

        ttk.Label(tab, textvariable=self._replay_status_var, anchor="w").pack(fill=tk.X)

    def _start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return

        tickers = parse_ticker_tuple(self._tickers_var.get())
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

        _clear_tree(self._table)
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

    def _start_recording(self) -> None:
        if self._record_worker is not None and self._record_worker.is_alive():
            return

        tickers = parse_ticker_tuple(self._record_tickers_var.get())
        if not tickers:
            messagebox.showerror("Missing tickers", "Enter one or more market tickers.")
            return

        duration_sec: float | None
        duration_text = self._record_duration_var.get().strip()

        if duration_text:
            try:
                duration_sec = float(duration_text)
            except ValueError:
                messagebox.showerror("Invalid duration", "Duration must be blank or a number.")
                return

            if duration_sec <= 0:
                messagebox.showerror("Invalid duration", "Duration must be greater than zero.")
                return
        else:
            duration_sec = None

        output_text = self._record_output_var.get().strip()
        output_dir = Path(output_text) if output_text else None

        self._clear_record_log()
        self._set_record_running(True)
        self._record_status_var.set("Connecting...")
        self._record_stop_event.clear()
        self._record_worker = threading.Thread(
            target=_record_worker_main,
            args=(
                self._record_events,
                self._record_stop_event,
                tickers,
                self._record_prod_var.get(),
                duration_sec,
                output_dir,
            ),
            daemon=True,
        )
        self._record_worker.start()

    def _stop_recording(self) -> None:
        self._record_stop_event.set()
        self._record_status_var.set("Stopping...")
        self._record_stop_button.configure(state=tk.DISABLED)

    def _start_replay(self) -> None:
        if self._replay_worker is not None and self._replay_worker.is_alive():
            return

        try:
            recording = require_recording_path(
                self._replay_recording_var.get().strip(),
                root=ROOT,
            )
        except ValueError as error:
            messagebox.showerror("Invalid recording", str(error))
            return

        try:
            speed = float(self._replay_speed_var.get())
            refresh = float(self._replay_refresh_var.get())
            latency_sec = float(self._replay_latency_var.get())
            order_size = parse_count_fp(self._replay_order_size_var.get())
            max_position = parse_count_fp(self._replay_max_position_var.get())
        except ValueError as error:
            messagebox.showerror("Invalid backtest setting", str(error))
            return

        if speed < 0:
            messagebox.showerror("Invalid speed", "Speed must be non-negative.")
            return

        if refresh <= 0:
            messagebox.showerror("Invalid refresh", "Refresh must be greater than zero.")
            return

        if latency_sec < 0:
            messagebox.showerror("Invalid latency", "Latency must be non-negative.")
            return

        if order_size <= 0:
            messagebox.showerror("Invalid order size", "Order size must be greater than zero.")
            return

        if max_position < 0:
            messagebox.showerror("Invalid max position", "Max position must be non-negative.")
            return

        self._clear_replay_tables()
        self._set_replay_running(True)
        self._replay_status_var.set("Running...")
        self._replay_stop_event.clear()
        self._replay_worker = threading.Thread(
            target=_replay_worker_main,
            args=(
                self._replay_events,
                self._replay_stop_event,
                recording,
                self._replay_strategy_var.get(),
                self._replay_fill_model_var.get(),
                speed,
                refresh,
                order_size,
                max_position,
                latency_sec,
            ),
            daemon=True,
        )
        self._replay_worker.start()

    def _stop_replay(self) -> None:
        self._replay_stop_event.set()
        self._replay_status_var.set("Stopping...")
        self._replay_stop_button.configure(state=tk.DISABLED)

    def _browse_record_output(self) -> None:
        path = filedialog.askdirectory(mustexist=False)

        if path:
            self._record_output_var.set(path)

    def _browse_replay_recording(self) -> None:
        path = filedialog.askdirectory(mustexist=True)

        if path:
            self._replay_recording_var.set(path)

    def _use_latest_recording(self) -> None:
        try:
            self._replay_recording_var.set(str(latest_recording_dir(ROOT)))
        except ValueError as error:
            messagebox.showerror("No recordings", str(error))

    def _on_close(self) -> None:
        self._stop_event.set()
        self._record_stop_event.set()
        self._replay_stop_event.set()
        self.destroy()

    def _poll_events(self) -> None:
        self._poll_live_events()
        self._poll_record_events()
        self._poll_replay_events()
        self.after(50, self._poll_events)

    def _poll_live_events(self) -> None:
        for event, payload in _drain_events(self._events):
            if event == "rows":
                _update_book_rows(self._table, payload)
            elif event == "status":
                self._status_var.set(payload)
            elif event == "error":
                self._status_var.set("Error")
                self._set_running(False)
                messagebox.showerror("Kalshi orderbook viewer", payload)
            elif event == "stopped":
                self._set_running(False)
                self._status_var.set("Stopped")

    def _poll_record_events(self) -> None:
        for event, payload in _drain_events(self._record_events):
            if event == "status":
                self._record_status_var.set(payload)
                self._append_record_log(payload)
            elif event == "error":
                self._record_status_var.set("Error")
                self._set_record_running(False)
                self._append_record_log(payload)
                messagebox.showerror("Kalshi recorder", payload)
            elif event == "stopped":
                self._set_record_running(False)
                self._record_status_var.set("Stopped")
            elif event == "recorded":
                self._record_status_var.set(payload)
                self._append_record_log(payload)

    def _poll_replay_events(self) -> None:
        for event, payload in _drain_events(self._replay_events):
            if event == "update":
                self._apply_backtest_update(payload)
            elif event == "result":
                self._apply_backtest_result(payload)
            elif event == "error":
                self._replay_status_var.set("Error")
                self._set_replay_running(False)
                messagebox.showerror("Kalshi backtest", payload)
            elif event == "stopped":
                self._set_replay_running(False)
                if self._replay_stop_event.is_set():
                    self._replay_status_var.set("Stopped")

    def _set_running(self, running: bool) -> None:
        _set_button_pair_running(self._start_button, self._stop_button, running)

    def _set_record_running(self, running: bool) -> None:
        _set_button_pair_running(
            self._record_start_button,
            self._record_stop_button,
            running,
        )

    def _set_replay_running(self, running: bool) -> None:
        _set_button_pair_running(
            self._replay_start_button,
            self._replay_stop_button,
            running,
        )

    def _clear_record_log(self) -> None:
        self._record_log.configure(state=tk.NORMAL)
        self._record_log.delete("1.0", tk.END)
        self._record_log.configure(state=tk.DISABLED)

    def _append_record_log(self, line: str) -> None:
        self._record_log.configure(state=tk.NORMAL)
        self._record_log.insert(tk.END, line + "\n")
        self._record_log.see(tk.END)
        self._record_log.configure(state=tk.DISABLED)

    def _clear_replay_tables(self) -> None:
        _clear_tree(self._replay_table)
        _clear_tree(self._summary_table)
        _clear_tree(self._fills_table)
        self._replay_seen_fills.clear()

    def _apply_backtest_update(self, update: BacktestUpdate) -> None:
        _update_book_rows(self._replay_table, update.rows)
        self._update_summary(update.summary)
        self._append_fills(update)
        self._replay_status_var.set(
            f"Events {update.summary.event_count} | fills {update.summary.fill_count}"
        )

    def _apply_backtest_result(self, result: BacktestResult) -> None:
        _update_book_rows(self._replay_table, result.final_rows)
        self._update_summary(result.summary)
        self._append_result_fills(result)
        self._replay_status_var.set(
            f"Completed {result.summary.event_count} events | fills {result.summary.fill_count}"
        )

    def _update_summary(self, summary: BacktestSummary) -> None:
        _clear_tree(self._summary_table)

        for metric, value in backtest_summary_rows(summary):
            self._summary_table.insert("", tk.END, values=(metric, value))

    def _append_fills(self, update: BacktestUpdate) -> None:
        self._append_fill_rows(update.recent_fills)

    def _append_result_fills(self, result: BacktestResult) -> None:
        self._append_fill_rows(result.fills)

    def _append_fill_rows(self, fills: Iterable[Any]) -> None:
        for fill in fills:
            self._append_fill_row(fill)

        self._trim_fill_rows()

    def _append_fill_row(self, fill: Any) -> None:
        if fill.fill_id in self._replay_seen_fills:
            return

        self._replay_seen_fills.add(fill.fill_id)
        self._fills_table.insert(
            "",
            tk.END,
            iid=fill.fill_id,
            values=(
                fill.observed_at_utc or "",
                fill.market_ticker,
                fill.action,
                format_price_fp(fill.yes_price),
                format_contract_count(fill.count),
                fill.reason,
            ),
        )

    def _trim_fill_rows(self) -> None:
        while len(self._fills_table.get_children("")) > MAX_FILL_ROWS:
            oldest = self._fills_table.get_children("")[0]
            self._fills_table.delete(oldest)
            self._replay_seen_fills.discard(oldest)


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
    environment = settings.environment(prod=prod)
    controller = FeedController(
        rest=KalshiRestClient(environment.rest_base_url, auth),
        ws=KalshiWebSocketClient(environment.ws_url, auth),
    )
    receiver: asyncio.Task[None] | None = None

    try:
        events.put(("status", f"Connecting to {environment.name}..."))
        await controller.connect()
        events.put(("status", "Subscribing..."))
        await controller.subscribe(tickers, channels=(ORDERBOOK_CHANNEL,))
        receiver = asyncio.create_task(controller.run_forever())
        events.put(("status", "Live"))

        while not stop_event.is_set():
            events.put(("rows", top_of_book_rows(controller.orderbooks, tickers)))
            await asyncio.sleep(refresh)
    except InvalidStatus as error:
        if getattr(error.response, "status_code", None) == 401:
            raise RuntimeError(
                f"Kalshi rejected websocket auth for {environment.name} (HTTP 401). "
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


def _record_worker_main(
    events: queue.Queue[Event],
    stop_event: threading.Event,
    tickers: tuple[str, ...],
    prod: bool,
    duration_sec: float | None,
    output_dir: Path | None,
) -> None:
    try:
        asyncio.run(_run_recording(events, stop_event, tickers, prod, duration_sec, output_dir))
    except Exception as error:
        events.put(("error", str(error)))


async def _run_recording(
    events: queue.Queue[Event],
    stop_event: threading.Event,
    tickers: tuple[str, ...],
    prod: bool,
    duration_sec: float | None,
    output_dir: Path | None,
) -> None:
    settings = load_settings()
    environment = settings.environment(prod=prod)
    auth = KalshiAuth(settings.api_key_id, settings.private_key_path)
    writer = RecordingSessionWriter.create(output_dir or default_recording_dir(ROOT))
    controller = FeedController(
        rest=KalshiRestClient(environment.rest_base_url, auth),
        ws=RecordingWebSocketClient(
            KalshiWebSocketClient(environment.ws_url, auth),
            writer,
        ),
    )
    receiver: asyncio.Task[None] | None = None

    try:
        events.put(("status", f"Connecting to {environment.name}..."))
        await controller.connect()
        events.put(("status", f"Subscribing to {len(tickers)} market(s)..."))
        await controller.subscribe(tickers, channels=(ORDERBOOK_CHANNEL,))
        writer.write_manifest(
            RecordingManifest.create(
                environment=environment.name,
                tickers=tickers,
                channels=(ORDERBOOK_CHANNEL,),
                price_ranges_by_ticker=controller.price_ranges_by_ticker,
                event_file=writer.event_path.name,
                started_at_utc=writer.started_at_utc,
                metadata={
                    "rest_base_url": environment.rest_base_url,
                    "ws_url": environment.ws_url,
                },
            )
        )
        events.put(("status", f"Recording to {writer.directory}"))
        receiver = asyncio.create_task(controller.run_forever())
        started = time.monotonic()

        while not stop_event.is_set():
            if duration_sec is not None and time.monotonic() - started >= duration_sec:
                break

            await asyncio.sleep(0.1)
    except InvalidStatus as error:
        if getattr(error.response, "status_code", None) == 401:
            raise RuntimeError(
                f"Kalshi rejected websocket auth for {environment.name} (HTTP 401). "
                "Use production keys with Production checked, or demo keys with it unchecked."
            ) from error

        raise
    finally:
        try:
            if receiver is not None:
                receiver.cancel()
                with suppress(asyncio.CancelledError):
                    await receiver
        finally:
            await controller.close()
            writer.finalize()
            writer.close()
            events.put(("recorded", f"Wrote {writer.event_count} event(s) to {writer.event_path}"))
            events.put(("stopped", None))


def _replay_worker_main(
    events: queue.Queue[Event],
    stop_event: threading.Event,
    recording: Path,
    strategy_name: str,
    fill_model_name: str,
    speed: float,
    refresh: float,
    order_size: int,
    max_position: int,
    latency_sec: float,
) -> None:
    try:
        strategy = strategy_from_name(
            strategy_name,
            count=order_size,
            max_position=max_position,
        )
        fill_model = fill_model_from_name(fill_model_name)

        def on_update(update: BacktestUpdate) -> None:
            events.put(("update", update))

        result = asyncio.run(
            run_replay_backtest(
                recording,
                strategy=strategy,
                fill_model=fill_model,
                speed_multiplier=speed,
                latency_seconds=latency_sec,
                on_update=on_update,
                update_interval_seconds=refresh,
                stop_requested=stop_event.is_set,
            )
        )
        events.put(("result", result))
        events.put(("stopped", None))
    except Exception as error:
        events.put(("error", str(error)))


def _configure_book_table(table: ttk.Treeview) -> None:
    headings = {
        "ticker": "Ticker",
        "bid": "Best Bid",
        "bid_size": "Bid Size",
        "ask": "Best Ask",
        "ask_size": "Ask Size",
    }
    widths = {
        "ticker": 360,
        "bid": 120,
        "bid_size": 110,
        "ask": 120,
        "ask_size": 110,
    }

    for column in headings:
        table.heading(column, text=headings[column])
        table.column(column, width=widths[column], anchor=tk.E)

    table.column("ticker", anchor=tk.W)


def _update_book_rows(table: ttk.Treeview, rows: tuple[Row, ...]) -> None:
    existing = set(table.get_children(""))

    for row in rows:
        ticker = row[0]
        if ticker in existing:
            table.item(ticker, values=row)
            existing.remove(ticker)
        else:
            table.insert("", tk.END, iid=ticker, values=row)

    for ticker in existing:
        table.delete(ticker)


def _clear_tree(table: ttk.Treeview) -> None:
    for item in table.get_children(""):
        table.delete(item)


def _drain_events(events: queue.Queue[Event]) -> Iterator[Event]:
    while True:
        try:
            yield events.get_nowait()
        except queue.Empty:
            return


def _set_button_pair_running(
    start_button: ttk.Button,
    stop_button: ttk.Button,
    running: bool,
) -> None:
    start_button.configure(state=tk.DISABLED if running else tk.NORMAL)
    stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)


def main() -> None:
    OrderbookViewer().mainloop()


if __name__ == "__main__":
    main()
