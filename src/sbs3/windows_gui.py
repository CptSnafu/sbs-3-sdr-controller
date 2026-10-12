"""Windows control surface styled after the BaseStation SDR panel.

Only the Tuner A/B command is currently sent to hardware. The six receiver
strips deliberately keep their state local until every field of their command
family (source, mute, squelch, and level) is independently confirmed.
"""

import json
import os
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

from .commands import digital_audio_mixer, mixer_configuration, sdr5_modulation_configuration, sdr_receiver_configuration, sdr_squelch_update, tuner_configuration
from .device_worker import DeviceRequest, PlannedFrame, SBS3DeviceWorker
from .ftdi import find_sbs3_control_device, list_devices
from .network import DEFAULT_SBS3_HOST, DEFAULT_SBS3_PORT, DeviceEndpoint
from .protocol import encode_frame
from .status import StatusSample
from .acars import AcarsRecord
from .windows_audio import WindowsDigitalAudioPlayer


BLACK, GREEN, RED, BLUE = "#050505", "#00ed31", "#ff2525", "#296cff"


class SBS3WindowsApp:
    def __init__(self, preview: bool = False) -> None:
        self.root = tk.Tk()
        self.root.title("SBS-3 SDR Control")
        self.root.minsize(720, 620)
        self.root.configure(bg="#ececec")
        self._detected_device_index = None
        self._state = self._load_state()
        self.sequence = self._state["next_sequence"]
        self.sequence_text = tk.StringVar(value=self._sequence_label())
        self.prepare_ftdi = tk.BooleanVar(value=True)
        self.preview = preview
        self._preset_name = tk.StringVar()
        self._preset_frequency = tk.StringVar()
        self._preset_boxes: list[ttk.Combobox] = []
        self._sdr_values: list[dict[str, tk.Variable]] = []
        self._debounce_ids: dict[str, str] = {}
        self._rssi_text: list[tk.StringVar] = []
        self._rssi_meters: list[tk.Canvas] = []
        self._tuner_active: dict[str, tk.BooleanVar] = {}
        self._digital_buttons: dict[int, tk.Button] = {}
        self._digital_receiver: Optional[int] = None
        self._digital_player = WindowsDigitalAudioPlayer()
        self._last_status = None
        self._device_worker = SBS3DeviceWorker(self.sequence, self._received_status, self._received_acars, self._received_audio)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    @staticmethod
    def _state_path() -> Path:
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        elif os.name == "posix":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path.home()
        return base / "SBS3Control" / "session.json"

    def _load_state(self) -> dict:
        defaults = {
            "next_sequence": 0x4A,
            "presets": [{"name": "Vienna Radar", "frequency": "129.050"}, {"name": "Airband test", "frequency": "134.675"}],
            "tuners": {"A": {"frequency": "125.000", "enabled": True}, "B": {"frequency": "123.800", "enabled": False}},
            "sdrs": [{} for _ in range(6)],
            "connection": {"transport": "usb", "host": DEFAULT_SBS3_HOST, "port": DEFAULT_SBS3_PORT},
        }
        try:
            saved = json.loads(self._state_path().read_text())
            defaults["next_sequence"] = int(saved.get("next_sequence", defaults["next_sequence"])) & 0xFF
            for tuner in ("A", "B"):
                defaults["tuners"][tuner].update(saved.get("tuners", {}).get(tuner, {}))
            if isinstance(saved.get("presets"), list):
                defaults["presets"] = [item for item in saved["presets"] if isinstance(item, dict) and item.get("name") and item.get("frequency")]
            for index, item in enumerate(saved.get("sdrs", [])[:6]):
                if isinstance(item, dict):
                    defaults["sdrs"][index].update(item)
            if isinstance(saved.get("connection"), dict):
                defaults["connection"].update(saved["connection"])
        except (OSError, ValueError, TypeError):
            pass
        return defaults

    def _save_state(self) -> None:
        try:
            if self._sdr_values:
                self._state["sdrs"] = [
                    {key: value.get() for key, value in item.items()}
                    for item in self._sdr_values
                ]
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._state["next_sequence"] = self.sequence
            path.write_text(json.dumps(self._state, indent=2))
        except OSError as error:
            self.append(f"Could not save next sequence: {error}")

    def _sequence_label(self) -> str:
        return f"Next sequence: 0x{self.sequence:02X}"

    def _preset_labels(self) -> list[str]:
        return [f"{item['name']} — {item['frequency']}" for item in self._state["presets"]]

    def _refresh_preset_boxes(self) -> None:
        for box in self._preset_boxes:
            box["values"] = self._preset_labels()

    def add_preset(self) -> None:
        name, frequency = self._preset_name.get().strip(), self._preset_frequency.get().strip().replace(",", ".")
        try:
            if not name or not 0 <= float(frequency) <= 4294.967295:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid preset", "Enter a name and valid MHz frequency.")
            return
        self._state["presets"] = [item for item in self._state["presets"] if item["name"] != name]
        self._state["presets"].append({"name": name, "frequency": frequency})
        self._preset_name.set("")
        self._preset_frequency.set("")
        self._refresh_preset_boxes()
        self._save_state()

    def _preset_box(self, parent: tk.Misc, frequency: tk.StringVar, send=None) -> ttk.Combobox:
        box = ttk.Combobox(parent, values=self._preset_labels(), width=17, state="readonly")
        self._preset_boxes.append(box)
        def selected(_event: object) -> None:
            try:
                frequency.set(self._state["presets"][box.current()]["frequency"])
                if send:
                    send()
            except (IndexError, tk.TclError):
                return
        box.bind("<<ComboboxSelected>>", selected)
        return box

    @staticmethod
    def _digital(parent: tk.Misc, variable: tk.Variable, size: int) -> tk.Label:
        return tk.Label(parent, textvariable=variable, bg=BLACK, fg=GREEN, anchor="e", font=("Consolas", size, "bold"))

    def _build(self) -> None:
        self.root.configure(bg="#101827")
        outer = tk.Frame(self.root, bg="#101827", padx=14, pady=12)
        outer.pack(fill="both", expand=True)
        header = tk.Frame(outer, bg="#101827")
        header.pack(fill="x", pady=(0, 12))
        tk.Label(header, text="SBS-3", bg="#101827", fg="#f7fafc", font=("Segoe UI", 20, "bold")).pack(side="left")
        tk.Label(header, text="SDR CONTROL", bg="#101827", fg="#38bdf8", font=("Segoe UI", 10, "bold")).pack(side="left", padx=10)
        tk.Button(header, text="⚙ Admin", command=self.show_admin, bg="#26354d", fg="#f7fafc", relief="flat").pack(side="right", padx=(6, 0))
        tk.Button(header, text="▤ Device Log", command=self.show_log, bg="#26354d", fg="#f7fafc", relief="flat").pack(side="right")
        tk.Button(header, text="✈ ACARS", command=self.show_acars, bg="#26354d", fg="#f7fafc", relief="flat").pack(side="right", padx=(0, 6))
        tk.Button(header, text="⌁ Connection", command=self.show_connection, bg="#26354d", fg="#f7fafc", relief="flat").pack(side="right", padx=(0, 6))

        top = tk.Frame(outer, bg="#101827")
        top.pack(fill="x", pady=(0, 7))
        tuner_area = tk.Frame(top, bg="#101827")
        tuner_area.pack(fill="x")
        tuner_area.columnconfigure((0, 1), weight=1)
        self._tuner_card(tuner_area, "A", 0)
        self._tuner_card(tuner_area, "B", 1)

        tk.Label(outer, text="RECEIVERS", bg="#101827", fg="#94a3b8", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 4))
        station_area = tk.Frame(outer, bg="#101827")
        station_area.pack(fill="both", expand=True)
        station_area.columnconfigure(0, weight=1)
        for number, frequency in enumerate(("119.400", "123.800", "121.600", "121.775", "134.675", "125.175"), start=1):
            self._station_strip(station_area, number, frequency)
        self._build_log_window()
        self._build_acars_window()


    def _tuner_card(self, parent: tk.Misc, tuner: str, column: int) -> None:
        card = tk.Frame(parent, bg=BLACK, bd=2, relief="sunken", padx=8, pady=6)
        card.grid(row=0, column=column, sticky="nsew", padx=(0, 5) if tuner == "A" else (5, 0))
        card.columnconfigure(1, weight=1)
        tuner_label = tk.Label(card, text=f"Tuner {tuner}", bg=BLACK, fg=GREEN, font=("Segoe UI", 11, "bold"))
        tuner_label.grid(row=0, column=0, sticky="w")
        saved = self._state["tuners"][tuner]
        try:
            display_frequency = f"{float(str(saved['frequency']).replace(',', '.')):.3f}"
        except (TypeError, ValueError):
            display_frequency = str(saved["frequency"])
        frequency, active = tk.StringVar(value=display_frequency), tk.BooleanVar(value=bool(saved["enabled"]))
        self._tuner_active[tuner] = active
        frequency_line = tk.Frame(card, bg=BLACK)
        frequency_line.grid(row=0, column=1, sticky="ew", padx=(12, 0))
        entry = tk.Entry(frequency_line, textvariable=frequency, bg=BLACK, fg=GREEN, insertbackground=GREEN, relief="flat", justify="right", font=("Consolas", 19, "bold"), width=10)
        entry.pack(side="left", fill="x", expand=True)
        tk.Label(frequency_line, text="MHz", bg=BLACK, fg=GREEN, font=("Consolas", 9, "bold")).pack(side="right", padx=(5, 1))
        def normalize_and_send() -> None:
            try:
                frequency.set(f"{float(frequency.get().replace(',', '.')):.3f}")
            except ValueError:
                pass
            self.send_tuner(tuner, frequency, active)
        entry.bind("<Return>", lambda _event: normalize_and_send())
        self._preset_box(card, frequency, normalize_and_send).grid(row=1, column=0, sticky="w", pady=(3, 0))
        button = tk.Button(card, width=8, command=lambda: self.request_tuner_toggle(tuner, frequency, active), bg="#26354d", relief="flat")
        button.grid(row=1, column=1, sticky="e", pady=(3, 0))
        def paint(*_args):
            on = active.get()
            entry.configure(fg=GREEN if on else "#4b5563")
            tuner_label.configure(fg=GREEN if on else "#94a3b8")
            button.configure(text="ON" if on else "OFF", fg="#22c55e" if on else "#94a3b8")
            if self._last_status is not None:
                self._paint_receiver_levels(self._last_status)
        active.trace_add("write", paint)
        paint()

    def _build_log_window(self) -> None:
        self.log_window = tk.Toplevel(self.root)
        self.log_window.title("SBS-3 Device Log")
        self.log_window.geometry("680x380")
        self.log_window.protocol("WM_DELETE_WINDOW", self.log_window.withdraw)
        self.log = tk.Text(self.log_window, wrap="word", state="disabled", bg="#0b1220", fg="#d1fae5", insertbackground="#d1fae5", font=("Consolas", 10))
        self.log.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_window.withdraw()

    def show_log(self) -> None:
        self.log_window.deiconify()
        self.log_window.lift()

    def _build_acars_window(self) -> None:
        self.acars_window = tk.Toplevel(self.root)
        self.acars_window.title("SBS-3 ACARS Messages")
        self.acars_window.geometry("920x400")
        self.acars_window.minsize(760, 230)
        self.acars_window.configure(bg="#101827")
        self.acars_window.protocol("WM_DELETE_WINDOW", self.acars_window.withdraw)
        header = tk.Frame(self.acars_window, bg="#101827", padx=10, pady=8)
        header.pack(fill="x")
        tk.Label(header, text="ACARS MESSAGES", bg="#101827", fg="#38bdf8", font=("Segoe UI", 12, "bold")).pack(side="left")
        tk.Label(
            header,
            text="Live records decoded by the SBS-3",
            bg="#101827",
            fg="#94a3b8",
            font=("Segoe UI", 9),
        ).pack(side="left", padx=10)
        tk.Button(header, text="Clear", command=self._clear_acars, bg="#26354d", fg="#f7fafc", relief="flat").pack(side="right")
        style = ttk.Style(self.acars_window)
        style.configure("Acars.Treeview", background=BLACK, fieldbackground=BLACK, foreground=GREEN, rowheight=22, font=("Consolas", 9))
        style.configure("Acars.Treeview.Heading", background="#f3f481", foreground="#172033", font=("Segoe UI", 9, "bold"))
        columns = ("date", "time", "source", "mode", "registration", "sequence", "flight", "message")
        self.acars_tree = ttk.Treeview(self.acars_window, columns=columns, show="headings", height=14, style="Acars.Treeview")
        headings = {
            "date": "Date", "time": "Time", "source": "Source", "mode": "Mode", "registration": "Registration",
            "sequence": "Sequence", "flight": "Flight", "message": "Message text",
        }
        widths = {"date": 75, "time": 70, "source": 150, "mode": 52, "registration": 100, "sequence": 85, "flight": 90, "message": 300}
        for column in columns:
            self.acars_tree.heading(column, text=headings[column])
            self.acars_tree.column(column, width=widths[column], minwidth=widths[column] // 2, anchor="w", stretch=column == "message")
        scroll = ttk.Scrollbar(self.acars_window, orient="vertical", command=self.acars_tree.yview)
        self.acars_tree.configure(yscrollcommand=scroll.set)
        self.acars_tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        scroll.pack(side="right", fill="y", padx=(0, 10), pady=(0, 10))
        self.acars_window.withdraw()

    def show_acars(self) -> None:
        self.acars_window.deiconify()
        self.acars_window.lift()

    def show_connection(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("SBS-3 Connection")
        window.resizable(False, False)
        form = ttk.Frame(window, padding=16)
        form.pack(fill="both", expand=True)
        transport = tk.StringVar(value=str(self._state["connection"].get("transport", "usb")))
        host = tk.StringVar(value=str(self._state["connection"].get("host", DEFAULT_SBS3_HOST)))
        port = tk.StringVar(value=str(self._state["connection"].get("port", DEFAULT_SBS3_PORT)))
        ttk.Label(form, text="Connection", font=("Segoe UI", 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Radiobutton(form, text="USB (automatic SBS-3 detection)", variable=transport, value="usb").grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(form, text="Network (SBS-3 Ethernet API)", variable=transport, value="network").grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 10))
        ttk.Label(form, text="Address").grid(row=3, column=0, sticky="w", pady=(0, 5))
        ttk.Entry(form, textvariable=host, width=24).grid(row=3, column=1, sticky="ew", pady=(0, 5))
        ttk.Label(form, text="TCP port").grid(row=4, column=0, sticky="w")
        ttk.Entry(form, textvariable=port, width=10).grid(row=4, column=1, sticky="w")
        ttk.Label(form, text="Network mode is used on the next command. USB and Ethernet are not used at the same time.", wraplength=350).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 10))
        check_status = tk.StringVar(value="")
        ttk.Label(form, textvariable=check_status, wraplength=350).grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 8))

        def check_network() -> None:
            import socket
            check_status.set("Checking TCP connection…")
            def run() -> None:
                try:
                    endpoint = DeviceEndpoint.network(host.get(), int(port.get()))
                    with socket.create_connection((endpoint.host, endpoint.port), timeout=2.0):
                        message = f"Network reachable: {endpoint.host}:{endpoint.port}. No SBS-3 data was sent."
                except (OSError, ValueError) as error:
                    message = f"Network check failed: {error}"
                self.root.after(0, lambda: (check_status.set(message), self.append(message)))
            import threading
            threading.Thread(target=run, daemon=True).start()
        def save() -> None:
            try:
                value = transport.get()
                if value not in {"usb", "network"}:
                    raise ValueError("Choose USB or Network.")
                if value == "network":
                    network_port = int(port.get())
                    DeviceEndpoint.network(host.get(), network_port)
                else:
                    network_port = DEFAULT_SBS3_PORT
            except ValueError as error:
                messagebox.showerror("Invalid connection", str(error), parent=window)
                return
            self._state["connection"] = {"transport": value, "host": host.get().strip(), "port": network_port}
            self._detected_device_index = None
            self._save_state()
            self.append(f"Saved SBS-3 connection: {'USB automatic detection' if value == 'usb' else f'network {host.get().strip()}:{network_port}'}. It will be used on the next command.")
            window.destroy()
        ttk.Button(form, text="NW Check", command=check_network).grid(row=7, column=0, sticky="w")
        ttk.Button(form, text="Save", command=save).grid(row=7, column=1, sticky="e")

    def _clear_acars(self) -> None:
        self.acars_tree.delete(*self.acars_tree.get_children())

    def _append_acars(self, record: AcarsRecord) -> None:
        message = record.message_text.replace("\r", "").replace("\n", " ↵ ") or "—"
        source = f"SDR {record.receiver_index + 1}"
        if 0 <= record.receiver_index < len(self._sdr_values):
            source += f" · {self._sdr_values[record.receiver_index]['frequency'].get()} MHz"
        self.acars_tree.insert(
            "", "end",
            values=(
                datetime.now().strftime("%Y-%m-%d"),
                datetime.now().strftime("%H:%M:%S"),
                source,
                record.mode or "—",
                record.registration or "—",
                record.sequence or "—",
                record.flight_number or "—",
                message,
            ),
        )
        children = self.acars_tree.get_children()
        if len(children) > 500:
            self.acars_tree.delete(*children[:len(children) - 500])
        self.acars_tree.yview_moveto(1.0)

    def show_admin(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("SBS-3 Admin")
        window.geometry("460x380")
        ttk.Label(window, text="Frequency presets", font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=14, pady=(14, 6))
        items = tk.Listbox(window, height=10)
        items.pack(fill="both", expand=True, padx=14)
        for item in self._preset_labels():
            items.insert("end", item)
        form = ttk.Frame(window, padding=14)
        form.pack(fill="x")
        ttk.Entry(form, textvariable=self._preset_name, width=24).grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(form, textvariable=self._preset_frequency, width=12).grid(row=0, column=1, padx=(0, 6))
        def refresh():
            items.delete(0, "end")
            for label in self._preset_labels():
                items.insert("end", label)
        ttk.Button(form, text="Add / update", command=lambda: (self.add_preset(), refresh())).grid(row=0, column=2)
        def delete_selected():
            selected = items.curselection()
            if selected:
                del self._state["presets"][selected[0]]
                self._refresh_preset_boxes()
                self._save_state()
                refresh()
        ttk.Button(form, text="Delete", command=delete_selected).grid(row=1, column=2, pady=(6, 0))
        ttk.Separator(window, orient="horizontal").pack(fill="x", padx=14, pady=(4, 10))
        ttk.Label(window, text="SBS-3 audio recovery", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=14)
        ttk.Label(
            window,
            text="Restores the known analogue and digital mixer levels. Tuner and SDR settings are not changed.",
            wraplength=420,
        ).pack(anchor="w", padx=14, pady=(2, 6))
        ttk.Button(window, text="Restore SBS-3 Audio Mixer", command=self.restore_audio_mixer).pack(anchor="w", padx=14)

    def restore_audio_mixer(self) -> None:
        if not self._hardware_commands_available():
            return
        if self._digital_receiver is not None:
            self.append("Stop direct digital audio before restoring the SBS-3 audio mixer.")
            return
        try:
            endpoint = self._resolve_endpoint()
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("SBS-3 audio recovery", str(error))
            return
        self.append("Queued restore of SBS-3 analogue and digital audio mixers.")
        levels = [100] * 6
        def build(sequence: int) -> list[PlannedFrame]:
            analogue = mixer_configuration(levels, sequence, output=0)
            digital_sequence = (sequence + 1) & 0xFF
            digital = mixer_configuration(levels, digital_sequence, output=1)
            return [
                PlannedFrame("Analogue audio mixer", encode_frame(analogue.payload, analogue.integrity), sequence),
                PlannedFrame("Digital audio mixer", encode_frame(digital.payload, digital.integrity), digital_sequence),
            ]
        def completed(lines: list[str], next_sequence: int, confirmed: bool) -> None:
            self.root.after(0, lambda: self._finish_generic(lines, next_sequence, confirmed))
        self._device_worker.submit(endpoint, self.prepare_ftdi.get(), DeviceRequest("Restore audio mixer", build, completed))

    def _station_strip(self, parent: tk.Misc, number: int, default_frequency: str) -> None:
        row = tk.Frame(parent, bg="#d0d0d0", bd=1, relief="groove", padx=4, pady=2)
        row.grid(row=number - 1, column=0, sticky="nsew", padx=3, pady=3)
        saved = self._state["sdrs"][number - 1]
        source = tk.StringVar(value=saved.get("source", "A"))
        mode = tk.StringVar(value=saved.get("mode", "AM"))
        frequency = tk.StringVar(value=saved.get("frequency", default_frequency))
        muted = tk.BooleanVar(value=bool(saved.get("muted", False)))
        confirmed_squelch = {1: 74, 2: 76, 3: 68, 4: 79, 5: 79, 6: 82}
        squelch = tk.IntVar(value=int(saved.get("squelch", confirmed_squelch[number])))
        volume = tk.IntVar(value=int(saved.get("volume", 75)))
        volume_text = tk.StringVar(value=f"Vol {volume.get()}%")
        volume.trace_add("write", lambda *_: volume_text.set(f"Vol {volume.get()}%"))
        self._sdr_values.append({"source": source, "mode": mode, "frequency": frequency, "muted": muted, "squelch": squelch, "volume": volume})
        squelch_text = tk.StringVar(value=f"SQ {squelch.get()}%")
        squelch.trace_add("write", lambda *_: squelch_text.set(f"SQ {squelch.get()}%"))
        left = tk.Frame(row, bg="#d0d0d0", width=58)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 3))
        left.grid_propagate(False)
        tk.Label(left, text=f"SDR {number}", bg="#d0d0d0", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        choice = tk.Frame(row, bg="#d0d0d0", width=55)
        choice.grid(row=0, column=1, sticky="nsw", padx=(0, 3))
        tk.Label(choice, text="Tuner", bg="#d0d0d0", font=("Segoe UI", 7, "bold")).pack(anchor="w")
        for name in ("A", "B"):
            tk.Radiobutton(choice, text=name, value=name, variable=source, command=lambda: self.send_sdr(number - 1, source, mode, frequency, muted, squelch), bg="#d0d0d0", selectcolor="#d0d0d0", activebackground="#d0d0d0", font=("Segoe UI", 8), padx=0).pack(anchor="w")
        modes = tk.Frame(row, bg="#d0d0d0", width=52)
        modes.grid(row=0, column=2, sticky="nsw", padx=(0, 3))
        tk.Label(modes, text="Mode", bg="#d0d0d0", font=("Segoe UI", 7, "bold")).pack(anchor="w")
        for name in ("AM", "FM"):
            tk.Radiobutton(modes, text=name, value=name, variable=mode, command=lambda: self.send_sdr(number - 1, source, mode, frequency, muted, squelch), bg="#d0d0d0", fg=BLUE, selectcolor="#d0d0d0", activebackground="#d0d0d0", font=("Segoe UI", 8, "bold"), padx=1).pack(anchor="w")
        radio = tk.Frame(row, bg=BLACK, bd=2, relief="sunken")
        radio.grid(row=0, column=3, sticky="nsw", padx=(0, 4))
        radio.grid_propagate(False)
        # The contents are packed, so pack propagation—not grid propagation—
        # must be disabled to keep every SDR frequency card exactly alike.
        radio.pack_propagate(False)
        radio.configure(width=360, height=76)
        frequency_line = tk.Frame(radio, bg=BLACK)
        frequency_line.pack(fill="x", padx=5, pady=(1, 0))
        entry = tk.Entry(frequency_line, textvariable=frequency, bg=BLACK, fg=GREEN, insertbackground=GREEN, relief="flat", justify="right", font=("Consolas", 17, "bold"), width=10)
        entry.pack(side="left", fill="x", expand=True)
        tk.Label(frequency_line, text="MHz", bg=BLACK, fg=GREEN, font=("Consolas", 9, "bold")).pack(side="right", padx=(5, 1))
        entry.bind("<Return>", lambda _event: self.send_sdr(number - 1, source, mode, frequency, muted, squelch))
        info = tk.Frame(radio, bg=BLACK)
        info.pack(fill="x", padx=7, pady=(0, 1))
        rssi = tk.StringVar(value="RSSI ---")
        self._rssi_text.append(rssi)
        preset = self._preset_box(info, frequency, lambda: self.send_sdr(number - 1, source, mode, frequency, muted, squelch))
        preset.configure(width=12)
        preset.pack(side="right")
        tk.Label(info, textvariable=rssi, bg=BLACK, fg=GREEN, anchor="w", font=("Consolas", 8)).pack(side="left")
        meter = tk.Canvas(radio, height=5, bg="#172033", highlightthickness=0)
        meter.pack(fill="x", padx=7, pady=(0, 2))
        meter.create_rectangle(0, 0, 0, 5, fill=GREEN, outline="", tags="level")
        self._rssi_meters.append(meter)
        right = tk.Frame(row, bg="#d0d0d0", width=145)
        right.grid(row=0, column=4, sticky="nse")
        right.columnconfigure((0, 1), weight=1)
        tk.Label(right, textvariable=squelch_text, bg="#d0d0d0", font=("Segoe UI", 8, "bold")).grid(row=0, column=0, sticky="w")
        tk.Scale(right, from_=0, to=100, orient="horizontal", length=85, showvalue=False, variable=squelch, command=lambda _value: self._debounce(f"sq-{number}", lambda: self.send_sdr(number - 1, source, mode, frequency, muted, squelch), 350), bg="#d0d0d0", highlightthickness=0).grid(row=0, column=1, padx=(2, 0))
        mute_button = tk.Button(right, width=7, command=lambda: self._toggle_mute(muted, mute_button, lambda: self.send_sdr(number - 1, source, mode, frequency, muted, squelch)))
        mute_button.grid(row=2, column=0, sticky="ew", pady=(2, 0))
        self._paint_mute(muted, mute_button)
        digital_button = tk.Button(right, text="DIGITAL", width=7, command=lambda: self.toggle_digital_output(number - 1))
        digital_button.grid(row=2, column=1, sticky="ew", padx=(3, 0), pady=(2, 0))
        self._digital_buttons[number - 1] = digital_button
        tk.Button(right, text="Send", width=6, command=lambda: self.send_sdr(number - 1, source, mode, frequency, muted, squelch)).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        tk.Label(right, textvariable=volume_text, bg="#d0d0d0", font=("Segoe UI", 8, "bold")).grid(row=1, column=0, sticky="w")
        tk.Scale(right, from_=0, to=100, orient="horizontal", length=85, showvalue=False, variable=volume, command=lambda _value: self._debounce(f"vol-{number}", lambda: self.send_sdr(number - 1, source, mode, frequency, muted, squelch), 350), bg="#d0d0d0", highlightthickness=0).grid(row=1, column=1, padx=(2, 0))

    @staticmethod
    def _paint_mute(value: tk.BooleanVar, button: tk.Button) -> None:
        button.configure(fg=RED if value.get() else GREEN, text="MUTE" if value.get() else "SIGNAL")

    def _toggle_mute(self, value: tk.BooleanVar, button: tk.Button, send: callable) -> None:
        value.set(not value.get())
        self._paint_mute(value, button)
        send()

    def _paint_digital_buttons(self) -> None:
        for receiver, button in self._digital_buttons.items():
            active = receiver == self._digital_receiver
            button.configure(
                text="STOP" if active else "DIGITAL",
                fg="#296cff" if active else "#26354d",
                state="normal" if self._digital_receiver is None or active else "disabled",
            )

    def toggle_digital_output(self, receiver: int) -> None:
        if not self._hardware_commands_available():
            return
        if self._digital_receiver is not None and self._digital_receiver != receiver:
            self.append(f"Digital audio is active for SDR {self._digital_receiver + 1}. Stop it before selecting another SDR.")
            return
        try:
            endpoint = self._resolve_endpoint()
            if endpoint.transport != "network":
                raise ValueError("Digital audio to this PC requires the SBS-3 Network connection.")
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("SBS-3 digital audio", str(error))
            return
        enabled = self._digital_receiver is None
        label = "Digital audio" if enabled else "Digital audio stop"
        self.append(f"Queued {label.lower()} for SDR {receiver + 1}.")
        def build(sequence: int) -> list[PlannedFrame]:
            frame = digital_audio_mixer(receiver if enabled else None, enabled, sequence)
            return [PlannedFrame(label, encode_frame(frame.payload, frame.integrity), sequence)]
        def completed(lines: list[str], next_sequence: int, confirmed: bool) -> None:
            self.root.after(0, lambda: self._finish_digital(lines, next_sequence, confirmed, receiver, enabled))
        self._device_worker.submit(endpoint, False, DeviceRequest(label, build, completed, network_login=enabled))

    def _finish_digital(self, lines: list[str], next_sequence: int, confirmed: bool, receiver: int, enabled: bool) -> None:
        self._finish_generic(lines, next_sequence, confirmed)
        if not confirmed:
            return
        try:
            if enabled:
                self._digital_player.start()
                self._digital_receiver = receiver
                self.append(f"Receiving direct digital audio from SDR {receiver + 1}.")
            else:
                self._digital_player.stop()
                self._digital_receiver = None
                self.append("Digital audio disabled.")
        except RuntimeError as error:
            self.append(str(error))
        self._paint_digital_buttons()

    def _debounce(self, key: str, action: callable, delay_ms: int) -> None:
        previous = self._debounce_ids.get(key)
        if previous:
            self.root.after_cancel(previous)
        self._debounce_ids[key] = self.root.after(delay_ms, action)

    def append(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def probe(self) -> None:
        try:
            devices = list_devices()
        except RuntimeError as error:
            self.append(str(error))
            return
        for device in devices:
            marker = "  <-- SBS-3 candidate" if device.is_sbs3 else ""
            self.append(f"[{device.index}] {device.vendor_id:04X}:{device.product_id:04X} {device.description!r}{marker}")

    def _resolve_endpoint(self) -> DeviceEndpoint:
        """Resolve the selected USB or Ethernet control endpoint."""
        connection = self._state["connection"]
        if connection.get("transport") == "network":
            endpoint = DeviceEndpoint.network(str(connection.get("host", "")), int(connection.get("port", DEFAULT_SBS3_PORT)))
            self.append(f"Using SBS-3 network API at {endpoint.host}:{endpoint.port}.")
            return endpoint
        if self._detected_device_index is not None:
            return DeviceEndpoint.usb(self._detected_device_index)
        device = find_sbs3_control_device(list_devices())
        self._detected_device_index = device.index
        self.append(f"Found SBS-3 control interface automatically: {device.description} (FTDI entry {device.index}).")
        return DeviceEndpoint.usb(device.index)

    def _hardware_commands_available(self) -> bool:
        """Keep the macOS USB preview safe while allowing real TCP control."""
        if not self.preview or self._state["connection"].get("transport") == "network":
            return True
        self.append("Mac preview: USB commands are available only on Windows. Select Network to control an SBS-3 over Ethernet.")
        return False

    def request_tuner_toggle(self, tuner: str, frequency: tk.StringVar, active: tk.BooleanVar) -> None:
        self.send_tuner(tuner, frequency, active, enabled=not active.get())

    def send_tuner(self, tuner: str, frequency: tk.StringVar, active: tk.BooleanVar, enabled: Optional[bool] = None) -> None:
        if not self._hardware_commands_available():
            return
        if self._digital_receiver is not None:
            self.append("Stop direct digital audio before changing tuner settings.")
            return
        try:
            endpoint = self._resolve_endpoint()
            hertz = round(float(frequency.get().replace(",", ".")) * 1_000_000)
            if not 0 <= hertz <= 0xFFFFFFFF:
                raise ValueError
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("SBS-3 not ready", str(error) if str(error) else "Enter a valid frequency.")
            return
        enabled = active.get() if enabled is None else enabled
        self.append(f"Queued Tuner {tuner} {'on' if enabled else 'off'} at {hertz} Hz.")
        def build(sequence: int) -> list[PlannedFrame]:
            frame = tuner_configuration(tuner, hertz, enabled, sequence)
            return [PlannedFrame(f"Tuner {tuner}", encode_frame(frame.payload, frame.integrity), sequence)]
        def completed(lines: list[str], next_sequence: int, confirmed: bool) -> None:
            self.root.after(0, lambda: self._finish_tuner(lines, next_sequence, confirmed, tuner, hertz, enabled))
        self._device_worker.submit(endpoint, self.prepare_ftdi.get(), DeviceRequest(f"Tuner {tuner}", build, completed))

    def send_sdr(self, receiver: int, source: tk.StringVar, mode: tk.StringVar, frequency: tk.StringVar, muted: tk.BooleanVar, squelch: tk.IntVar) -> None:
        """Send the fully captured SDR source/mode/squelch/mute configuration."""
        if not self._hardware_commands_available():
            return
        if self._digital_receiver is not None:
            self.append("Stop direct digital audio before changing SDR settings.")
            return
        try:
            endpoint = self._resolve_endpoint()
            hertz = round(float(frequency.get().replace(",", ".")) * 1_000_000)
            if not 0 <= hertz <= 0xFFFFFFFF:
                raise ValueError
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("SBS-3 not ready", str(error) if str(error) else "Enter a valid SDR frequency.")
            return
        source_value, am = source.get(), mode.get() == "AM"
        squelch_value, muted_value = squelch.get(), muted.get()
        volumes = [int(item["volume"].get()) for item in self._sdr_values]
        self.append(f"Queued SDR {receiver + 1}: Tuner {source_value}, {'AM' if am else 'FM'}, {hertz} Hz, squelch {squelch_value}, {'mute' if muted_value else 'unmuted'}.")
        def build(sequence: int) -> list[PlannedFrame]:
            frame = sdr_receiver_configuration(receiver, hertz, am, sequence, source=source_value, squelch=squelch_value, muted=muted_value)
            update_sequence = (sequence + 1) & 0xFF
            update = sdr_squelch_update(receiver, squelch_value, update_sequence)
            mixer_sequence = (sequence + 2) & 0xFF
            mixer = mixer_configuration(volumes, mixer_sequence)
            return [
                PlannedFrame(f"SDR {receiver + 1}", encode_frame(frame.payload, frame.integrity), sequence),
                PlannedFrame(f"SDR {receiver + 1} squelch", encode_frame(update.payload, update.integrity), update_sequence),
                PlannedFrame("Audio mixer", encode_frame(mixer.payload, mixer.integrity), mixer_sequence),
            ]
        def completed(lines: list[str], next_sequence: int, confirmed: bool) -> None:
            self.root.after(0, lambda: self._finish_sdr(lines, next_sequence, confirmed, receiver, source_value, am, hertz, squelch_value, muted_value, volumes))
        self._device_worker.submit(endpoint, self.prepare_ftdi.get(), DeviceRequest(f"SDR {receiver + 1}", build, completed))

    def quick_test_134675(self) -> None:
        """Apply the two captured commands needed for the first audio test."""
        if not self._hardware_commands_available():
            return
        try:
            endpoint = self._resolve_endpoint()
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("SBS-3 not ready", str(error))
            return
        self.append("Queued quick test: Tuner A and SDR 5 at 134.675 MHz AM.")
        def build(sequence: int) -> list[PlannedFrame]:
            first = tuner_configuration("A", 134_675_000, True, sequence)
            second_sequence = (sequence + 1) & 0xFF
            second = sdr5_modulation_configuration(True, second_sequence)
            return [
                PlannedFrame("Tuner A", encode_frame(first.payload, first.integrity), sequence),
                PlannedFrame("SDR 5 AM", encode_frame(second.payload, second.integrity), second_sequence),
            ]
        def completed(lines: list[str], next_sequence: int, confirmed: bool) -> None:
            self.root.after(0, lambda: self._finish_generic(lines, next_sequence, confirmed))
        self._device_worker.submit(endpoint, self.prepare_ftdi.get(), DeviceRequest("Quick test", build, completed))

    def _finish_generic(self, lines: list[str], next_sequence: int, confirmed: bool) -> None:
        self.sequence = next_sequence
        for line in lines:
            self.append(line)
        self.append("Command confirmed." if confirmed else "Command was not fully confirmed.")
        self.sequence_text.set(self._sequence_label())
        self._save_state()

    def _finish_tuner(self, lines: list[str], next_sequence: int, confirmed: bool, tuner: str, hertz: int, enabled: bool) -> None:
        self._finish_generic(lines, next_sequence, confirmed)
        if confirmed:
            self._state["tuners"][tuner] = {"frequency": f"{hertz / 1_000_000:.3f}", "enabled": enabled}
            self._tuner_active[tuner].set(enabled)
            self._save_state()

    def _finish_sdr(self, lines: list[str], next_sequence: int, confirmed: bool, receiver: int, source: str, am: bool, hertz: int, squelch: int, muted: bool, volumes: list[int]) -> None:
        self._finish_generic(lines, next_sequence, confirmed)
        if confirmed:
            self._state["sdrs"][receiver] = {
                "source": source, "mode": "AM" if am else "FM", "frequency": f"{hertz / 1_000_000:.6f}",
                "squelch": squelch, "muted": muted, "volume": volumes[receiver],
            }
            self._save_state()

    def _received_status(self, sample: StatusSample) -> None:
        self.root.after(0, lambda: self._paint_receiver_levels(sample))

    def _received_acars(self, record: AcarsRecord) -> None:
        self.root.after(0, lambda: self._append_acars(record))

    def _received_audio(self, pcm: bytes) -> None:
        self._digital_player.enqueue(pcm)

    def _paint_receiver_levels(self, sample: StatusSample) -> None:
        # The device emits background measurement values even with RF disabled.
        # Display them only for receivers whose selected source tuner is on.
        self._last_status = sample
        for receiver, (level, text, meter) in enumerate(zip(sample.receiver_levels, self._rssi_text, self._rssi_meters)):
            source = self._sdr_values[receiver]["source"].get()
            tuner = self._tuner_active.get(source)
            width = max(meter.winfo_width(), 1)
            if tuner is None or not tuner.get():
                text.set("RSSI ---")
                meter.coords("level", 0, 0, 0, 5)
                continue
            percent = max(0, min(100, round((level - 76) * 100 / 29)))
            text.set(f"RSSI {level:03d}")
            meter.coords("level", 0, 0, width * percent / 100, 5)

    def _close(self) -> None:
        self._digital_player.stop()
        self._device_worker.close()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def launch() -> None:
    SBS3WindowsApp(preview=os.name != "nt").run()
