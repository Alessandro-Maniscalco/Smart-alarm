"""Core logic for Smart Alarm (no GUI).

Install deps: pip install requests python-dotenv
"""
import os, time, subprocess, sys, json, select, tty, termios
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv

# --- Config ---
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
TZ = ZoneInfo("Europe/Athens")

# --- Core: live ETA ---
def get_eta_seconds(origin: str, destination: str) -> int:
    api_key = os.getenv("GOOGLE_MAPS_API_KEY") or GOOGLE_API_KEY
    if not api_key:
        raise RuntimeError("Set GOOGLE_MAPS_API_KEY in your environment.")
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": origin,
        "destinations": destination,
        "mode": "driving",
        "departure_time": "now",
        "traffic_model": "best_guess",
        "key": api_key,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    print(r)
    data = r.json()
    print(f"data: {data}")
    try:
        elem = data["rows"][0]["elements"][0]
        if elem.get("status") != "OK":
            raise RuntimeError(f"Element not OK: {json.dumps(elem)}")
        return int((elem.get("duration_in_traffic") or elem["duration"])["value"])
    except Exception as e:
        raise RuntimeError(f"Bad Distance Matrix response: {json.dumps(data)[:600]}") from e

def _parse_arrival(arrival_iso: str) -> datetime:
    dt = datetime.fromisoformat(arrival_iso)
    # If user passed a naive time, interpret it in TZ; else convert to TZ
    return (dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ))

def compute_wake_time(arrival_iso: str, prep_min: int, buffer_min: int,
                      origin: str, destination: str) -> dict:
    now = datetime.now(TZ)
    arrival = _parse_arrival(arrival_iso)
    eta_s = get_eta_seconds(origin, destination)
    depart_latest = arrival - timedelta(seconds=eta_s) - timedelta(minutes=buffer_min)
    wake_time = depart_latest - timedelta(minutes=prep_min)
    return {
        "now": now.isoformat(),
        "arrival": arrival.isoformat(),
        "eta_seconds": eta_s,
        "depart_latest": depart_latest.isoformat(),
        "wake_time": wake_time.isoformat(),
        "wake_now": now >= wake_time,
    }

def ring_alarm(sound_path: str | None = None) -> str:
    # macOS: try to play the provided sound file; remove terminal-bell fallback
    if sound_path and os.path.exists(sound_path):
        print("Alarm triggered! Press any key to stop...")
        try:
            # Set up terminal for key detection once
            if sys.platform == "darwin":  # macOS
                import tty
                import termios
                # Save terminal settings
                old_settings = termios.tcgetattr(sys.stdin.fileno())
                try:
                    # Set terminal to raw mode
                    tty.setraw(sys.stdin.fileno())
                    
                    stop_requested = False
                    for i in range(10):
                        if stop_requested:
                            break
                        # Start playback in a subprocess we can terminate
                        proc = subprocess.Popen(["afplay", sound_path])
                        print(f"Alarm #{i+1}/10")
                        # While sound is playing, poll for keypress
                        while proc.poll() is None:
                            if select.select([sys.stdin], [], [], 0.05)[0]:
                                key = sys.stdin.read(1)
                                print(f"Alarm stopped by key press: {repr(key)}")
                                # Try to stop playback immediately
                                try:
                                    proc.terminate()
                                    try:
                                        proc.wait(timeout=0.5)
                                    except Exception:
                                        proc.kill()
                                finally:
                                    stop_requested = True
                                break
                        # If process finished naturally and no stop requested, continue to next repeat
                        if stop_requested:
                            break
                    
                finally:
                    # Restore terminal settings
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            else:
                # Fallback for non-macOS
                for i in range(10):
                    subprocess.run(["afplay", sound_path], check=False)
                    print(f"Alarm #{i+1}/10")
                    time.sleep(1)
                    
        except FileNotFoundError:
            print("'afplay' not found; cannot play alarm sound.")
        except Exception as exc:
            print(f"Failed to play alarm sound: {exc}")
    else:
        print("No valid sound file provided; skipping alarm sound.")
    return "Alarm attempted"



# --- Orchestrator: polling loop ---
def run_alarm(origin: str,
              destination: str,
              arrival_iso: str,
              prep_min: int,
              buffer_min: int,
              sound_path: str | None = None,
              coarse_poll_s: int = 180,
              fine_poll_s: int = 60,
              fine_window_min: int = 30) -> None:
    tz = TZ
    last_wake_iso = None
    while True:
        info = compute_wake_time(arrival_iso, prep_min, buffer_min, origin, destination)
        wake_time = datetime.fromisoformat(info["wake_time"]).astimezone(tz)
        now = datetime.now(tz)

        if wake_time.isoformat() != last_wake_iso:
            eta_min = max(0, info["eta_seconds"] // 60)
            print(f"[{now.strftime('%H:%M:%S')}] ETA={eta_min} min; "
                  f"depart_latest={info['depart_latest']}; wake_time={info['wake_time']}")
            last_wake_iso = wake_time.isoformat()

        if now >= wake_time:
            print("Triggering alarm.")
            ring_alarm(sound_path)
            break

        remaining = (wake_time - now).total_seconds()
        if remaining <= fine_window_min * 60:
            sleep_s = min(fine_poll_s, max(15, int(remaining / 4)))
        else:
            sleep_s = max(15, int(coarse_poll_s))
        time.sleep(sleep_s)

if __name__ == "__main__":
    print("This module contains core logic only. Run the GUI with: python smart_alarm_app.py")