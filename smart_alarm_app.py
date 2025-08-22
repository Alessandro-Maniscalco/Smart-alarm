import os
import time
import threading
import queue
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime

from smart_alarm import compute_wake_time, TZ


class SmartAlarmApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Smart Alarm")
        self.geometry("860x750")

        # State
        self.state_var = tk.StringVar(value="Idle")  # Idle | Running | Ringing
        self.button_var = tk.StringVar(value="Run Program")
        self.google_api_key_var = tk.StringVar(value=os.getenv("GOOGLE_MAPS_API_KEY") or "")
        self.origin_var = tk.StringVar(value="Syntagma Square, Athens")
        self.destination_var = tk.StringVar(value="Athens International Airport")
        self.arrival_iso_var = tk.StringVar(value="2025-10-09T15:30:00+03:00")
        self.prep_min_var = tk.StringVar(value="15")
        self.buffer_min_var = tk.StringVar(value="60")
        self.sound_path_var = tk.StringVar(value="wakey-wakey-eggs.mp3")
        self.coarse_poll_s_var = tk.StringVar(value="180")
        self.fine_poll_s_var = tk.StringVar(value="60")
        self.fine_window_min_var = tk.StringVar(value="30")

        # Threading primitives
        self._worker_thread: threading.Thread | None = None
        self._stop_program_event = threading.Event()
        self._stop_alarm_event = threading.Event()
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._current_alarm_proc: subprocess.Popen | None = None

        self._build_ui()
        self.after(100, self._drain_log_queue)

    # UI builders
    def _build_ui(self) -> None:
        container = ttk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        # Inputs grid
        grid = ttk.Frame(container)
        grid.pack(fill=tk.X, pady=(0, 10))

        def add_row(row: int, label: str, widget: tk.Widget) -> None:
            ttk.Label(grid, text=label).grid(row=row, column=0, sticky=tk.W, padx=(0, 8), pady=4)
            widget.grid(row=row, column=1, sticky=tk.EW, pady=4)

        grid.columnconfigure(1, weight=1)

        add_row(0, "Google API Key", ttk.Entry(grid, textvariable=self.google_api_key_var, show="*"))
        add_row(1, "Origin", ttk.Entry(grid, textvariable=self.origin_var))
        add_row(2, "Destination", ttk.Entry(grid, textvariable=self.destination_var))
        add_row(3, "Arrival (ISO)", ttk.Entry(grid, textvariable=self.arrival_iso_var))
        add_row(4, "Prep Minutes", ttk.Entry(grid, textvariable=self.prep_min_var))
        add_row(5, "Buffer Minutes", ttk.Entry(grid, textvariable=self.buffer_min_var))

        # Sound file with browse
        sound_row = ttk.Frame(grid)
        sound_entry = ttk.Entry(sound_row, textvariable=self.sound_path_var)
        sound_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(sound_row, text="Browse...", command=self._browse_sound).pack(side=tk.LEFT, padx=(8, 0))
        add_row(6, "Alarm Sound", sound_row)

        add_row(7, "Coarse Poll (s)", ttk.Entry(grid, textvariable=self.coarse_poll_s_var))
        add_row(8, "Fine Poll (s)", ttk.Entry(grid, textvariable=self.fine_poll_s_var))
        add_row(9, "Fine Window (min)", ttk.Entry(grid, textvariable=self.fine_window_min_var))

        # Status row
        status_row = ttk.Frame(container)
        status_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(status_row, textvariable=self.state_var).pack(side=tk.LEFT)

        # Log area
        log_frame = ttk.Frame(container)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(log_frame, wrap=tk.WORD, height=20, state=tk.DISABLED)
        vsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Bottom controls
        bottom = ttk.Frame(container)
        bottom.pack(fill=tk.X, pady=(10, 0))
        self.primary_btn = ttk.Button(bottom, textvariable=self.button_var, command=self._on_primary_button)
        self.primary_btn.pack(side=tk.RIGHT)

        # Play startup alarm sound
        self.after(500, self._play_startup_sound)

    def _browse_sound(self) -> None:
        path = filedialog.askopenfilename(title="Choose alarm sound",
                                          filetypes=(("Audio files", "*.mp3 *.wav"), ("All files", "*.*")))
        if path:
            self.sound_path_var.set(path)

    # Logging
    def log(self, message: str) -> None:
        timestamp = datetime.now(TZ).strftime("%H:%M:%S")
        self._log_queue.put(f"[{timestamp}] {message}\n")

    def _drain_log_queue(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self.log_text.configure(state=tk.NORMAL)
                self.log_text.insert(tk.END, msg)
                self.log_text.see(tk.END)
                self.log_text.configure(state=tk.DISABLED)
        except queue.Empty:
            pass
        finally:
            self.after(150, self._drain_log_queue)

    # Primary button handler
    def _on_primary_button(self) -> None:
        state = self.state_var.get()
        if state == "Idle":
            self._start_program()
        elif state == "Running":
            self._stop_program()
        elif state == "Ringing":
            self._stop_alarm()

    # State transitions
    def _set_state(self, state: str, button_text: str) -> None:
        self.state_var.set(state)
        self.button_var.set(button_text)

    def _start_program(self) -> None:
        try:
            # Validate inputs
            google_key = self.google_api_key_var.get().strip()
            if google_key:
                os.environ["GOOGLE_MAPS_API_KEY"] = google_key

            origin = self.origin_var.get().strip()
            destination = self.destination_var.get().strip()
            arrival_iso = self.arrival_iso_var.get().strip()
            prep_min = int(self.prep_min_var.get().strip())
            buffer_min = int(self.buffer_min_var.get().strip())
            sound_path = self.sound_path_var.get().strip() or None
            coarse_poll_s = int(self.coarse_poll_s_var.get().strip())
            fine_poll_s = int(self.fine_poll_s_var.get().strip())
            fine_window_min = int(self.fine_window_min_var.get().strip())
        except Exception as exc:
            messagebox.showerror("Invalid input", f"Please check your inputs: {exc}")
            return

        if not origin or not destination or not arrival_iso:
            messagebox.showerror("Missing input", "Origin, Destination, and Arrival are required.")
            return

        self._stop_program_event.clear()
        self._stop_alarm_event.clear()

        args = (origin, destination, arrival_iso, prep_min, buffer_min, sound_path, coarse_poll_s, fine_poll_s, fine_window_min)
        self._worker_thread = threading.Thread(target=self._run_worker, args=args, daemon=True)
        self._worker_thread.start()
        self._set_state("Running", "Stop Program")
        self.log("Program started.")

    def _stop_program(self) -> None:
        self._stop_program_event.set()
        self._set_state("Idle", "Run Program")
        self.log("Program stopped.")

    def _stop_alarm(self) -> None:
        self._stop_alarm_event.set()
        # Terminate any active process immediately
        proc = self._current_alarm_proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=0.5)
                except Exception:
                    proc.kill()
            except Exception:
                pass
        self._current_alarm_proc = None
        self.log("Alarm stopped.")
        self._set_state("Idle", "Run Program")

    # Worker and helpers
    def _run_worker(self,
                    origin: str,
                    destination: str,
                    arrival_iso: str,
                    prep_min: int,
                    buffer_min: int,
                    sound_path: str | None,
                    coarse_poll_s: int,
                    fine_poll_s: int,
                    fine_window_min: int) -> None:
        next_poll_at = 0.0
        try:
            while not self._stop_program_event.is_set():

                now = time.time()
                if now < next_poll_at:
                    time.sleep(min(0.2, next_poll_at - now))
                    continue

                # Poll
                try:
                    info = compute_wake_time(arrival_iso, prep_min, buffer_min, origin, destination)
                except Exception as exc:
                    self.log(f"API error: {exc}")
                    # Retry later
                    next_poll_at = time.time() + max(15, coarse_poll_s)
                    continue

                wake_time_iso = info["wake_time"]
                eta_min = max(0, info["eta_seconds"] // 60)
                self.log(
                    f"ETA={eta_min} min; depart_latest={info['depart_latest']}; wake_time={wake_time_iso}"
                )

                from datetime import datetime as _dt
                now_dt = _dt.now(TZ)
                from datetime import datetime as _dt2
                target_dt = _dt2.fromisoformat(wake_time_iso).astimezone(TZ)

                if now_dt >= target_dt:
                    self.log("Triggering alarm.")
                    self.after(0, lambda: self._set_state("Ringing", "Stop Alarm"))
                    self._ring_alarm_gui(sound_path)
                    break

                remaining = (target_dt - now_dt).total_seconds()
                if remaining <= fine_window_min * 60:
                    sleep_s = min(fine_poll_s, max(15, int(remaining / 4)))
                else:
                    sleep_s = max(15, int(coarse_poll_s))
                next_poll_at = time.time() + sleep_s
                self.log(f"Next poll in {sleep_s}s")
        finally:
            # If not ringing, reset UI when loop ends
            if self.state_var.get() != "Ringing":
                self.after(0, lambda: self._set_state("Idle", "Run Program"))

    def _ring_alarm_gui(self, sound_path: str | None) -> None:
        if not sound_path or not os.path.exists(sound_path):
            self.log("No valid sound file provided; skipping alarm sound.")
            self.after(0, lambda: self._set_state("Idle", "Run Program"))
            return

        repeats = 10
        for i in range(repeats):
            if self._stop_alarm_event.is_set():
                break
            try:
                proc = subprocess.Popen(["afplay", sound_path])
                self._current_alarm_proc = proc
                self.log(f"Alarm #{i+1}/{repeats}")
                while proc.poll() is None:
                    if self._stop_alarm_event.is_set():
                        try:
                            proc.terminate()
                            try:
                                proc.wait(timeout=0.5)
                            except Exception:
                                proc.kill()
                        except Exception:
                            pass
                        break
                    time.sleep(0.05)
            except FileNotFoundError:
                self.log("'afplay' not found; cannot play alarm sound.")
                break
            except Exception as exc:
                self.log(f"Failed to play alarm sound: {exc}")
                break
        self._current_alarm_proc = None
        # After ringing completes or is stopped
        self.after(0, lambda: self._set_state("Idle", "Run Program"))

    def _play_startup_sound(self) -> None:
        """Play the startup alarm sound when the program starts using the existing function logic."""
        sound_path = self.sound_path_var.get().strip()
        if sound_path:
            try:
                # Use the same logic as try_startup_alarm_sound but without exiting
                startup_thread = threading.Thread(target=self._run_startup_sound_test, args=(sound_path,), daemon=True)
                startup_thread.start()
                self.log("Testing startup alarm sound...")
            except Exception as exc:
                self.log(f"Failed to start startup sound test: {exc}")
        else:
            self.log("No sound file configured for startup test.")

    def _run_startup_sound_test(self, sound_path: str) -> None:
        """Run the startup sound test using the existing function logic."""
        try:
            if not os.path.exists(sound_path):
                self.log(f"Startup sound test failed; file not found: {sound_path}")
                return
            
            self.log("Playing startup sound test for 3 seconds...")
            # Use the same logic as try_startup_alarm_sound but without exiting
            proc = subprocess.Popen(["afplay", "-t", "3", sound_path])
            proc.wait()
            self.log("Startup sound test successful!")
        except FileNotFoundError:
            self.log("'afplay' not found; cannot run startup sound test.")
        except Exception as exc:
            self.log(f"Startup sound test failed: {exc}")


if __name__ == "__main__":
    app = SmartAlarmApp()
    app.mainloop()

    