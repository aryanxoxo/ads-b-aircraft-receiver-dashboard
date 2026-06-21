#!/usr/bin/env python3
"""
Local ADS-B receiver dashboard server.

Inputs:
  ADSB_AIRCRAFT_JSON  Path to dump1090/readsb aircraft.json
  ADSB_SBS_HOST       Host for SBS/BaseStation TCP feed, usually 127.0.0.1
  ADSB_SBS_PORT       Port for SBS/BaseStation TCP feed, usually 30003
  ADSB_RECEIVER_LAT   Receiver latitude, default 49.2827
  ADSB_RECEIVER_LON   Receiver longitude, default -123.1207

Examples:
  python adsb_live_server.py --port 8770 --aircraft-json C:\\dump1090\\public_html\\data\\aircraft.json
  python adsb_live_server.py --port 8770 --sbs-host 127.0.0.1 --sbs-port 30003
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "adsb_logs"
LOG_DIR.mkdir(exist_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class ReceiverState:
    def __init__(self, receiver_lat: float, receiver_lon: float) -> None:
        self.receiver = {"lat": receiver_lat, "lon": receiver_lon}
        self.aircraft: dict[str, dict[str, Any]] = {}
        self.messages: deque[dict[str, Any]] = deque(maxlen=1200)
        self.source = "waiting"
        self.source_detail = "No ADS-B input configured yet"
        self.last_update: float | None = None
        self.frame_count = 0
        self.lock = threading.Lock()
        self.csv_path = LOG_DIR / f"mode_s_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        self._csv_file = self.csv_path.open("a", newline="", encoding="utf-8")
        self._csv = csv.DictWriter(
            self._csv_file,
            fieldnames=[
                "time",
                "df",
                "icao",
                "callsign",
                "altitude",
                "lat",
                "lon",
                "range_km",
                "speed",
                "heading",
                "rssi_db",
                "raw",
                "source",
            ],
        )
        self._csv.writeheader()

    def upsert_aircraft(self, ac: dict[str, Any], source: str) -> None:
        icao = str(ac.get("icao") or ac.get("hex") or "").upper().strip()
        if not icao:
            return

        lat = ac.get("lat")
        lon = ac.get("lon")
        range_value = None
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            range_value = round(distance_km(self.receiver["lat"], self.receiver["lon"], lat, lon), 1)

        normalized = {
            "icao": icao,
            "callsign": str(ac.get("callsign") or ac.get("flight") or icao).strip() or icao,
            "lat": lat,
            "lon": lon,
            "altitude": ac.get("altitude") or ac.get("alt_baro") or ac.get("alt_geom"),
            "speed": ac.get("speed") or ac.get("gs"),
            "heading": ac.get("heading") or ac.get("track") or 0,
            "rssi": ac.get("rssi") or ac.get("rssi_db") or ac.get("signal"),
            "type": ac.get("type") or ac.get("category") or "",
            "messages": ac.get("messages") or ac.get("seen_pos") or 0,
            "squawk": ac.get("squawk") or "",
            "range": range_value,
            "seen": ac.get("seen"),
            "updated": utc_now(),
            "source": source,
        }

        frame = {
            "time": normalized["updated"],
            "df": ac.get("df") or "DF17",
            "icao": icao,
            "callsign": normalized["callsign"],
            "altitude": normalized["altitude"],
            "lat": normalized["lat"],
            "lon": normalized["lon"],
            "range_km": normalized["range"],
            "speed": normalized["speed"],
            "heading": normalized["heading"],
            "rssi_db": normalized["rssi"],
            "raw": ac.get("raw") or "",
            "source": source,
        }

        with self.lock:
            previous = self.aircraft.get(icao, {})
            previous.update({k: v for k, v in normalized.items() if v not in (None, "")})
            self.aircraft[icao] = previous
            self.messages.appendleft(frame)
            self.frame_count += 1
            self.last_update = time.time()
            self._csv.writerow(frame)
            self._csv_file.flush()

    def set_source(self, source: str, detail: str) -> None:
        with self.lock:
            self.source = source
            self.source_detail = detail

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            aircraft = list(self.aircraft.values())
            messages = list(self.messages)[:200]
            last_update = self.last_update
            source = self.source
            source_detail = self.source_detail
            frame_count = self.frame_count

        now = time.time()
        fresh_aircraft = [
            ac
            for ac in aircraft
            if not ac.get("updated")
            or (now - datetime.fromisoformat(ac["updated"]).timestamp()) < 90
        ]
        max_range = max((ac.get("range") or 0 for ac in fresh_aircraft), default=0)
        age = None if last_update is None else round(now - last_update, 1)
        connected = bool(last_update and now - last_update < 15)

        return {
            "receiver": self.receiver,
            "status": {
                "connected": connected,
                "source": source,
                "source_detail": source_detail,
                "last_update_age_s": age,
                "frames": frame_count,
                "log_path": str(self.csv_path),
                "message": "Live ADS-B feed active" if connected else "Waiting for decoded ADS-B messages",
            },
            "stats": {
                "contacts": len(fresh_aircraft),
                "frames_per_sec": None,
                "max_range_km": round(max_range),
                "preamble_lock": None,
                "drift_ppm": None,
            },
            "aircraft": fresh_aircraft,
            "messages": messages,
        }


def poll_aircraft_json(state: ReceiverState, path: Path, interval: float) -> None:
    state.set_source("dump1090/readsb aircraft.json", str(path))
    last_seen: dict[str, int] = {}
    while True:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("aircraft", []):
                icao = str(item.get("hex", "")).upper()
                if not icao:
                    continue
                current_messages = int(item.get("messages") or 0)
                if current_messages == last_seen.get(icao) and "lat" not in item:
                    continue
                last_seen[icao] = current_messages
                item["icao"] = icao
                item["callsign"] = item.get("flight", "").strip() or icao
                state.upsert_aircraft(item, "dump1090")
        except FileNotFoundError:
            state.set_source("dump1090/readsb aircraft.json", f"Missing file: {path}")
        except Exception as exc:  # Keep receiver thread alive while hardware/software starts.
            state.set_source("dump1090/readsb aircraft.json", f"{path}: {exc}")
        time.sleep(interval)


def parse_sbs_line(line: str) -> dict[str, Any] | None:
    parts = line.strip().split(",")
    if len(parts) < 22 or parts[0] != "MSG":
        return None
    return {
        "df": f"MSG{parts[1]}",
        "icao": parts[4].upper(),
        "callsign": parts[10].strip(),
        "altitude": int(parts[11]) if parts[11] else None,
        "speed": int(parts[12]) if parts[12] else None,
        "heading": int(parts[13]) if parts[13] else None,
        "lat": float(parts[14]) if parts[14] else None,
        "lon": float(parts[15]) if parts[15] else None,
        "squawk": parts[17],
        "raw": line.strip(),
    }


def read_sbs_tcp(state: ReceiverState, host: str, port: int) -> None:
    state.set_source("SBS/BaseStation TCP", f"{host}:{port}")
    while True:
        try:
            with socket.create_connection((host, port), timeout=10) as sock:
                state.set_source("SBS/BaseStation TCP", f"Connected to {host}:{port}")
                buffer = ""
                while True:
                    chunk = sock.recv(4096).decode("utf-8", errors="ignore")
                    if not chunk:
                        break
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        parsed = parse_sbs_line(line)
                        if parsed:
                            state.upsert_aircraft(parsed, "sbs")
        except Exception as exc:
            state.set_source("SBS/BaseStation TCP", f"{host}:{port}: {exc}")
            time.sleep(3)


class Handler(SimpleHTTPRequestHandler):
    state: ReceiverState

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self.send_json(self.state.snapshot()["status"])
            return
        if parsed.path == "/api/aircraft":
            snap = self.state.snapshot()
            self.send_json(
                {
                    "receiver": snap["receiver"],
                    "status": snap["status"],
                    "stats": snap["stats"],
                    "aircraft": snap["aircraft"],
                }
            )
            return
        if parsed.path == "/api/messages":
            snap = self.state.snapshot()
            self.send_json({"status": snap["status"], "messages": snap["messages"]})
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve live ADS-B dashboard data")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8770")))
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--aircraft-json", default=os.getenv("ADSB_AIRCRAFT_JSON", ""))
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--sbs-host", default=os.getenv("ADSB_SBS_HOST", ""))
    parser.add_argument("--sbs-port", type=int, default=int(os.getenv("ADSB_SBS_PORT", "30003")))
    parser.add_argument("--receiver-lat", type=float, default=float(os.getenv("ADSB_RECEIVER_LAT", "49.2827")))
    parser.add_argument("--receiver-lon", type=float, default=float(os.getenv("ADSB_RECEIVER_LON", "-123.1207")))
    args = parser.parse_args()

    os.chdir(ROOT)
    state = ReceiverState(args.receiver_lat, args.receiver_lon)
    Handler.state = state

    if args.aircraft_json:
        thread = threading.Thread(
            target=poll_aircraft_json,
            args=(state, Path(args.aircraft_json), args.poll_interval),
            daemon=True,
        )
        thread.start()
    if args.sbs_host:
        thread = threading.Thread(target=read_sbs_tcp, args=(state, args.sbs_host, args.sbs_port), daemon=True)
        thread.start()
    if not args.aircraft_json and not args.sbs_host:
        state.set_source(
            "not configured",
            "Start with --aircraft-json path/to/aircraft.json or --sbs-host 127.0.0.1 --sbs-port 30003",
        )

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ADS-B dashboard: http://{args.host}:{args.port}/")
    print(f"Logging decoded frames to: {state.csv_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
