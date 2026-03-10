#!/usr/bin/env python3
"""FilaWizard Filament Inventory (Rebuilt Backend)

- Clean FilamentSlot + InventoryManager
- Safe hardware detection (PCA9548 mux + NAU7802 + PN532)
- Background loop works whether run via `python app.py` or `flask run`
- Keeps existing UI + NFC format + calibration + Excel export + history
- Prevents silent 500s: API endpoints return JSON with error details

NFC payload format:
  Brand|Color|Type|Tare
"""

import io
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List

from flask import Flask, jsonify, render_template, request, send_file

# Hardware libs (Pi)
import board
import busio
import smbus2
from adafruit_pn532.i2c import PN532_I2C

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

# ---------------------------
# Configuration
# ---------------------------

APP_TITLE = "FilaWizard"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "inventory_config.json")
HISTORY_FILE = os.path.join(BASE_DIR, "history.jsonl")

PCA9548_ADDRESSES = [0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76, 0x77]

# Environmental sensor wiring (optional)
HDC3022_ADDR = 0x44
HDC3022_MUX_INDEX = 0
HDC3022_CHANNEL = 7

# Device addresses
NAU7802_ADDR = 0x2A
PN532_ADDR = 0x24

# Business logic
LOW_FILAMENT_THRESHOLD_G = 250
NEW_ROLL_DEFAULT_G = 1000

# Loop
SCAN_INTERVAL_SEC = 2.0

# Stable muxed NAU7802 read tuning
MUX_SETTLE_SEC = 0.15
NAU_WARMUP_READS = 30
NAU_FILTER_SAMPLES = 40
NAU_SAMPLE_DELAY_SEC = 0.03

# Default slots: 48 (6 muxes x 8 channels). If fewer muxes are connected, slots reduce automatically.
DEFAULT_SLOT_COUNT = int(os.environ.get("SLOT_COUNT", "48"))

# ---------------------------
# Helpers
# ---------------------------

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def safe_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None

def clamp_text(s: str, max_len: int = 32) -> str:
    return (s or "").strip()[:max_len]

def calibration_status(last_calibrated: Optional[str]) -> str:
    if not last_calibrated:
        return "Not calibrated"
    try:
        from datetime import timezone, timedelta
        s = last_calibrated.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - dt > timedelta(days=365):
            return "Not calibrated"
        return "Calibrated"
    except Exception:
        return "Not calibrated"

def append_history(event: Dict[str, Any]) -> None:
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass

# ---------------------------
# Multiplexer
# ---------------------------

class PCA9548Multiplexer:
    def __init__(self, bus: smbus2.SMBus, address: int):
        self.bus = bus
        self.address = address
        self.available = self._check_available()

    def _check_available(self) -> bool:
        try:
            self.bus.write_byte(self.address, 0x00)
            return True
        except Exception:
            return False

    def select_channel(self, channel: int) -> bool:
        if not self.available:
            return False
        try:
            self.bus.write_byte(self.address, 1 << channel)
            return True
        except Exception:
            return False

    def disable_all(self) -> None:
        try:
            self.bus.write_byte(self.address, 0x00)
        except Exception:
            pass

# ---------------------------
# Environmental sensor (HDC3022) - optional
# ---------------------------

class HDC3022Sensor:
    def __init__(self, bus: smbus2.SMBus, mux: PCA9548Multiplexer, channel: int, address: int = HDC3022_ADDR):
        self.bus = bus
        self.mux = mux
        self.channel = channel
        self.address = address

    def read(self) -> Tuple[Optional[float], Optional[float]]:
        try:
            if not self.mux.select_channel(self.channel):
                return None, None
            time.sleep(0.01)
            self.bus.write_i2c_block_data(self.address, 0x24, [0x00])
            time.sleep(0.1)
            data = self.bus.read_i2c_block_data(self.address, 0x00, 6)
            temp_raw = (data[0] << 8) | data[1]
            hum_raw = (data[3] << 8) | data[4]
            temperature = -45 + 175 * (temp_raw / 65535.0)
            humidity = 100 * (hum_raw / 65535.0)
            return float(temperature), float(humidity)
        except Exception:
            return None, None
        finally:
            self.mux.disable_all()

# ---------------------------
# Load cell (NAU7802)
# ---------------------------

class NAU7802LoadCell:

    REG_PU_CTRL = 0x00
    REG_CTRL1   = 0x01
    REG_CTRL2   = 0x02
    REG_ADCO_B2 = 0x12
    REG_ADCO_B1 = 0x13
    REG_ADCO_B0 = 0x14

    BIT_RR  = 0x01
    BIT_PUD = 0x02
    BIT_PUA = 0x04
    BIT_PUR = 0x08
    BIT_CR  = 0x20

    def __init__(self, bus, mux, channel, address=NAU7802_ADDR):
        self.bus = bus
        self.mux = mux
        self.channel = channel
        self.address = address

        self.available = False
        self.initialized = False
        self.last_error = None

        self._init_device()

    def _select(self):
        ok = self.mux.select_channel(self.channel)
        if ok:
            time.sleep(MUX_SETTLE_SEC)
        return ok

    def _write(self, reg, val):
        if not self._select():
            return False
        try:
            self.bus.write_byte_data(self.address, reg, val)
            return True
        except Exception as e:
            self.last_error = str(e)
            return False
        finally:
            self.mux.disable_all()

    def _read(self, reg):
        if not self._select():
            return 0
        try:
            return self.bus.read_byte_data(self.address, reg)
        except Exception:
            return 0
        finally:
            self.mux.disable_all()

    def _wait_for_bit(self, reg, mask, timeout=1.5):
        start = time.time()
        while time.time() - start < timeout:
            if self._read(reg) & mask:
                return True
            time.sleep(0.02)
        return False

    def _probe(self):
        try:
            if not self._select():
                return False
            self.bus.read_byte_data(self.address, self.REG_PU_CTRL)
            return True
        except Exception:
            return False
        finally:
            self.mux.disable_all()

    def _init_device(self):

        self.available = self._probe()

        if not self.available:
            return

        # reset
        self._write(self.REG_PU_CTRL, self.BIT_RR)
        time.sleep(0.02)

        self._write(self.REG_PU_CTRL, 0x00)
        time.sleep(0.02)

        # power digital
        self._write(self.REG_PU_CTRL, self.BIT_PUD)

        # wait ready
        if not self._wait_for_bit(self.REG_PU_CTRL, self.BIT_PUR):
            self.last_error = "PUR not ready"
            return

        # power analog
        self._write(self.REG_PU_CTRL, self.BIT_PUD | self.BIT_PUA)

        time.sleep(0.1)

        # gain 128
        ctrl1 = self._read(self.REG_CTRL1)
        ctrl1 = (ctrl1 & 0xF8) | 0x07
        self._write(self.REG_CTRL1, ctrl1)

        # 10 SPS
        ctrl2 = self._read(self.REG_CTRL2)
        ctrl2 = (ctrl2 & 0x8F) | 0x00
        self._write(self.REG_CTRL2, ctrl2)

        # internal calibration
        self._write(self.REG_CTRL2, ctrl2 | 0x04)

        time.sleep(1)

        # discard warmup reads
        for _ in range(10):
            self.read_raw_once()

        self.initialized = True

    def data_ready(self):
        return bool(self._read(self.REG_PU_CTRL) & self.BIT_CR)

    def read_raw_once(self):

        if not (self.available and self.initialized):
            return None

        timeout = 100
        while not self.data_ready() and timeout > 0:
            time.sleep(0.02)
            timeout -= 1

        if timeout <= 0:
            return None

        if not self._select():
            return None

        try:
            b2 = self.bus.read_byte_data(self.address, self.REG_ADCO_B2)
            b1 = self.bus.read_byte_data(self.address, self.REG_ADCO_B1)
            b0 = self.bus.read_byte_data(self.address, self.REG_ADCO_B0)
        finally:
            self.mux.disable_all()

        value = (b2 << 16) | (b1 << 8) | b0

        if value & 0x800000:
            value -= 0x1000000

        return value

    def read_filtered_raw(self):

        if not (self.available and self.initialized):
            return None

        for _ in range(NAU_WARMUP_READS):
            self.read_raw_once()
            time.sleep(NAU_SAMPLE_DELAY_SEC)

        vals = []

        for _ in range(NAU_FILTER_SAMPLES):
            v = self.read_raw_once()
            if v is not None:
                vals.append(v)
            time.sleep(NAU_SAMPLE_DELAY_SEC)

        if not vals:
            return None

        vals.sort()

        mid = len(vals)//2

        return vals[mid]
   
# ---------------------------
# Slot + Config
# ---------------------------

@dataclass
class SlotConfig:
    # counts-per-gram (raw delta / grams)
    calibration_factor: float = 1.0
    # raw reading at zero load
    zero_offset_raw: float = 0.0
    last_calibrated: Optional[str] = None

class FilamentSlot:
    def __init__(self, slot_id: int, bus: smbus2.SMBus, mux: PCA9548Multiplexer, channel: int, i2c_shared: busio.I2C, cfg: SlotConfig):
        self.slot_id = slot_id
        self.bus = bus
        self.mux = mux
        self.channel = channel
        self.i2c_shared = i2c_shared
        self.cfg = cfg

        self.nau7802 = NAU7802LoadCell(bus=bus, mux=mux, channel=channel, address=NAU7802_ADDR)
        self.nfc: Optional[PN532_I2C] = None

        self.has_scale = bool(self.nau7802.available)
        self.nfc_available = False
        self.has_nfc = False
        self.hardware_present = bool(self.has_scale)

        # Hotplug tracking (RAM-only)
        self._was_present = self.hardware_present
        self._disconnect_seen = False

        # NFC / material state
        self.uid: Optional[str] = None
        self.filament_brand = "Unknown"
        self.filament_color = "N/A"
        self.filament_type = "N/A"
        self.tag_tare: Optional[float] = None
        self.nfc_needs_programming = False

        self.gross_weight = 0.0
        self.weight = 0.0
        self.is_active = False
        self.last_error: Optional[str] = None

        self._init_nfc()

    @property
    def calibration_factor(self) -> float:
        return float(self.cfg.calibration_factor or 1.0)

    def _init_nfc(self) -> None:
        try:
            if not self.mux.select_channel(self.channel):
                self.nfc_available = False
                return
            time.sleep(0.02)
            self.nfc = PN532_I2C(self.i2c_shared, debug=False, address=PN532_ADDR)
            self.nfc.SAM_configuration()
            self.nfc_available = True
            self.has_nfc = True
        except Exception as e:
            self.nfc = None
            self.nfc_available = False
            self.has_nfc = False
            self.last_error = f"NFC init: {e}"
        finally:
            self.mux.disable_all()
            self.hardware_present = bool(self.has_scale or self.has_nfc)
            self._was_present = self.hardware_present

    def _read_ntag_text(self) -> Optional[str]:
        if not self.nfc_available or not self.nfc:
            return None
        try:
            if not self.mux.select_channel(self.channel):
                return None
            raw = bytearray()
            for block in range(4, 8):
                data = self.nfc.ntag2xx_read_block(block)
                if not data:
                    break
                raw.extend(bytes(data))
            return raw.decode("utf-8", errors="ignore").strip("\x00").strip()
        except Exception:
            return None
        finally:
            self.mux.disable_all()

    def _write_ntag_text(self, text: str) -> bool:
        if not self.nfc_available or not self.nfc:
            return False
        payload = (text or "").encode("utf-8")[:64]
        payload = payload + b"\x00" * (64 - len(payload))
        try:
            if not self.mux.select_channel(self.channel):
                return False
            for i in range(4):
                chunk = payload[i*16:(i+1)*16]
                ok = self.nfc.ntag2xx_write_block(4 + i, bytearray(chunk))
                if not ok:
                    return False
            return True
        except Exception:
            return False
        finally:
            self.mux.disable_all()

    def refresh_hardware(self) -> None:
        prev = self._was_present

        # scale re-init if missing
        if not self.nau7802.available:
            self.nau7802 = NAU7802LoadCell(bus=self.bus, mux=self.mux, channel=self.channel, address=NAU7802_ADDR)
        self.has_scale = bool(self.nau7802.available)

        # NFC re-init if missing
        if not self.nfc_available:
            self._init_nfc()
        self.has_nfc = bool(self.nfc_available)

        self.hardware_present = bool(self.has_scale or self.has_nfc)

        if prev and not self.hardware_present:
            self._disconnect_seen = True
        if self._disconnect_seen and (not prev) and self.hardware_present:
            self.cfg.last_calibrated = None
            self._disconnect_seen = False

        self._was_present = self.hardware_present

    def read_nfc(self) -> bool:
        if not self.nfc_available or not self.nfc:
            self.uid = None
            self.nfc_needs_programming = False
            self.has_nfc = False
            self.hardware_present = bool(self.has_scale or self.has_nfc)
            return False

        try:
            if not self.mux.select_channel(self.channel):
                self.uid = None
                self.nfc_needs_programming = False
                return False

            uid_bytes = self.nfc.read_passive_target(timeout=0.4)
            if not uid_bytes:
                self.uid = None
                self.nfc_needs_programming = False
                if abs(self.weight) < 10:
                    self.is_active = False
                return False

            self.uid = "".join(f"{b:02X}" for b in uid_bytes)
            self.is_active = True

            txt = self._read_ntag_text()
            if not txt:
                self.nfc_needs_programming = True
                return True

            parts = [p.strip() for p in txt.split("|")]
            parsed_any = False
            if len(parts) >= 3 and parts[0] and parts[1] and parts[2]:
                self.filament_brand, self.filament_color, self.filament_type = parts[0], parts[1], parts[2]
                parsed_any = True

                tare = None
            if len(parts) >= 4 and parts[3] != "":
                tare = safe_float(parts[3])
            self.tag_tare = tare
            self.nfc_needs_programming = not parsed_any
            return True
        except Exception as e:
            self.last_error = f"NFC read: {e}"
            return False
        finally:
            self.mux.disable_all()

    def read_weight(self):

        if not self.nau7802.available:
            self.weight = 0.0
            self.gross_weight = 0.0
            return 0.0

        raw = self.nau7802.read_filtered_raw()

        if raw is None:
            self.weight = 0.0
            self.gross_weight = 0.0
            return 0.0

        counts_per_g = float(self.calibration_factor or 0.0)
        zero = float(self.cfg.zero_offset_raw or 0.0)

        gross = 0.0

        if counts_per_g != 0:
            gross = (raw - zero) / counts_per_g

        if gross < 0 and gross > -50:
            gross = 0.0

        self.gross_weight = float(gross)

        tare_g = float(self.tag_tare) if self.tag_tare else 0.0

        self.weight = float(self.gross_weight - tare_g)

        if abs(self.gross_weight) > 10 or self.uid:
            self.is_active = True

        return self.weight

    def write_tag(self, brand: str, color: str, filament_type: str, tare: Optional[float]) -> bool:
        if not self.uid:
            return False

        brand = clamp_text(brand or self.filament_brand or "Unknown")
        color = clamp_text(color or self.filament_color or "N/A")
        filament_type = clamp_text(filament_type or self.filament_type or "N/A")

        if tare is None:
            tare = self.tag_tare

        tare_str = ""
        if tare is not None:
            try:
                tare_str = str(round(float(tare), 2))
            except Exception:
                tare_str = ""

        payload = f"{brand}|{color}|{filament_type}|{tare_str}"
        ok = self._write_ntag_text(payload)
        if ok:
            self.filament_brand = brand
            self.filament_color = color
            self.filament_type = filament_type
            self.tag_tare = safe_float(tare_str) if tare_str != "" else None
            self.nfc_needs_programming = False
            self.is_active = True
        return ok

    def set_new_roll(self, full_roll_g: float, manual_tare: Optional[float] = None) -> bool:
        if manual_tare is not None:
            tare = max(0.0, float(manual_tare))
        else:
            tare = max(0.0, float(self.gross_weight) - float(full_roll_g))
        return self.write_tag(self.filament_brand, self.filament_color, self.filament_type, tare)

    def get_data(self) -> Dict[str, Any]:
        cal_stat = calibration_status(self.cfg.last_calibrated)
        return {
            "slot_id": self.slot_id,
            "hardware_present": bool(self.hardware_present),
            "has_scale": bool(self.has_scale),
            "has_nfc": bool(self.has_nfc),
            "uid": self.uid,
            "paired_uid": None,
            "requires_new_roll_check": bool(self.gross_weight >= 10 and (self.uid is None or self.nfc_needs_programming)),
            "nfc_needs_programming": bool(self.nfc_needs_programming),
            "brand": self.filament_brand,
            "color": self.filament_color,
            "type": self.filament_type,
            "tare": self.tag_tare,
            "gross_weight": round(float(self.gross_weight or 0.0), 1),
            "weight": round(float(self.weight or 0.0), 1),
            "is_active": bool(self.is_active),
            "calibration_factor": float(self.calibration_factor),
            "last_calibrated": self.cfg.last_calibrated,
            "calibration_status": cal_stat,
            "calibration_required": (cal_stat != "Calibrated"),
            "last_error": self.last_error,
        }

# ---------------------------
# Inventory Manager
# ---------------------------

class InventoryManager:
    def __init__(self, bus: smbus2.SMBus, i2c_shared: busio.I2C):
        self.lock = threading.Lock()
        self.bus = bus
        self.i2c_shared = i2c_shared

        self.muxes: List[PCA9548Multiplexer] = []
        for addr in PCA9548_ADDRESSES:
            mux = PCA9548Multiplexer(self.bus, addr)
            if mux.available:
                self.muxes.append(mux)

        self.sensor: Optional[HDC3022Sensor] = None
        if self.muxes and HDC3022_MUX_INDEX < len(self.muxes):
            self.sensor = HDC3022Sensor(self.bus, self.muxes[HDC3022_MUX_INDEX], HDC3022_CHANNEL, HDC3022_ADDR)

        self.temperature_c: Optional[float] = None
        self.humidity: Optional[float] = None

        self.config = self._load_config()
        self.slots: List[FilamentSlot] = []
        self._build_slots()

        self.last_error: Optional[str] = None

    def _build_slots(self) -> None:
        max_by_mux = len(self.muxes) * 8
        target = min(DEFAULT_SLOT_COUNT, max_by_mux) if max_by_mux > 0 else 0
        for slot_id in range(target):
            mux_index = slot_id // 8
            channel = slot_id % 8
            mux = self.muxes[mux_index]
            cfg = self.config.get(str(slot_id), SlotConfig())
            self.slots.append(FilamentSlot(slot_id, self.bus, mux, channel, self.i2c_shared, cfg))

    def _load_config(self) -> Dict[str, SlotConfig]:
        try:
            if not os.path.exists(CONFIG_FILE):
                return {}
            raw = json.loads(open(CONFIG_FILE, "r", encoding="utf-8").read())
            out: Dict[str, SlotConfig] = {}
            for k, v in (raw.get("slots") or {}).items():
                out[str(k)] = SlotConfig(
                    calibration_factor=float(v.get("calibration_factor", 1.0) or 1.0),
                    last_calibrated=v.get("last_calibrated"),
                )
            return out
        except Exception:
            return {}

    def _save_config(self) -> None:
        try:
            payload = {"slots": {str(s.slot_id): {
                "calibration_factor": float(s.cfg.calibration_factor),
                "last_calibrated": s.cfg.last_calibrated,
            } for s in self.slots}}
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            pass

    def get_slot(self, slot_id: int) -> FilamentSlot:
        return self.slots[int(slot_id)]

    def update_readings(self) -> None:
        with self.lock:
            try:
                if self.sensor:
                    self.temperature_c, self.humidity = self.sensor.read()

                if not hasattr(self, "_nfc_poll_counter"):
                    self._nfc_poll_counter = 0

                for s in self.slots:
                    try:
                        s.refresh_hardware()

                        if not s.hardware_present:
                            s.uid = None
                            s.is_active = False
                            s.gross_weight = 0.0
                            s.weight = 0.0
                            continue

                    # Always read weight
                        s.read_weight()

                    # Only poll NFC occasionally
                        if self._nfc_poll_counter % 5 == 0:
                            s.read_nfc()

                    except Exception as e:
                        s.last_error = f"slot: {e}"

                self._nfc_poll_counter += 1

                self._save_config()
                self.last_error = None

            except Exception as e:
                self.last_error = str(e)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            slots = [s.get_data() for s in self.slots if s.hardware_present]
            return {
                "slots": slots,
                "active_slots": len(slots),
                "present_ports": len(slots),
                "temperature": self.temperature_c,
                "humidity": self.humidity,
                "error": self.last_error,
            }

# ---------------------------
# Flask App + Background Loop
# ---------------------------

app = Flask(__name__)

# Shared buses
_bus = smbus2.SMBus(1)
_i2c = busio.I2C(board.SCL, board.SDA)

inventory = InventoryManager(_bus, _i2c)

_bg_started = False
_bg_lock = threading.Lock()

def start_background_loop() -> None:
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True

    def loop():
        while True:
            try:
                inventory.update_readings()
            except Exception:
                pass
            time.sleep(SCAN_INTERVAL_SEC)

    threading.Thread(target=loop, daemon=True).start()

# Start immediately so it works under `flask run` too.
start_background_loop()

# ---------------------------
# Routes
# ---------------------------

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/inventory")
def api_inventory():
    return jsonify(inventory.snapshot()), 200

@app.route("/api/status")
def api_status():
    snap = inventory.snapshot()
    return jsonify({
        "active_slots": snap.get("active_slots", 0),
        "present_ports": snap.get("present_ports", 0),
        "temperature": snap.get("temperature"),
        "humidity": snap.get("humidity"),
        "error": snap.get("error"),
    }), 200

@app.route("/api/program_nfc/<int:slot_id>", methods=["POST"])
def api_program_nfc(slot_id: int):
    data = request.get_json(silent=True) or {}
    brand = clamp_text(data.get("brand") or "")
    color = clamp_text(data.get("color") or "")
    filament_type = clamp_text(data.get("type") or "")
    tare = safe_float(data.get("tare"))

    with inventory.lock:
        slot = inventory.get_slot(slot_id)
        ok = slot.write_tag(brand, color, filament_type, tare)
        append_history({"ts": now_iso(), "slot": slot_id, "event": "program_nfc", "ok": ok, "uid": slot.uid})
        return jsonify({"success": ok, "slot": slot.get_data()}), (200 if ok else 400)

@app.route("/api/rewrite_nfc/<int:slot_id>", methods=["POST"])
def api_rewrite_nfc(slot_id: int):
    return api_program_nfc(slot_id)

@app.route("/api/replace_spool/<int:slot_id>", methods=["POST"])
def api_replace_spool(slot_id: int):
    data = request.get_json(silent=True) or {}
    full_roll_g = float(data.get("full_roll_g") or NEW_ROLL_DEFAULT_G)
    manual_tare = safe_float(data.get("manual_tare"))

    with inventory.lock:
        slot = inventory.get_slot(slot_id)
        slot.read_weight()
        ok = slot.set_new_roll(full_roll_g=full_roll_g, manual_tare=manual_tare)
        append_history({"ts": now_iso(), "slot": slot_id, "event": "replace_spool", "ok": ok, "uid": slot.uid, "full_roll_g": full_roll_g})
        return jsonify({"success": ok, "slot": slot.get_data()}), (200 if ok else 400)


@app.route("/api/zero/<int:slot_id>", methods=["POST"])
def api_zero(slot_id: int):
    """Capture current raw reading as zero offset (no load on scale)."""
    with inventory.lock:
        slot = inventory.get_slot(slot_id)
        if not slot.has_scale:
            return jsonify({"success": False, "error": "Load cell not detected"}), 400
        raw = None
        last_exc = None
        for _ in range(5):
            try:
                raw = slot.nau7802.read_filtered_raw()
                break
            except Exception as e:
                last_exc = e
                time.sleep(0.02)
        if raw is None:
            return jsonify({"success": False, "error": "Unable to read load cell", "details": str(last_exc) if last_exc else None}), 400
        if raw == 0:
            return jsonify({"success": False, "error": "Unable to read load cell"}), 400
        slot.cfg.zero_offset_raw = float(raw)
        inventory._save_config()
        append_history({"ts": now_iso(), "slot": slot_id, "event": "zero", "raw": raw})
        return jsonify({"success": True, "zero_offset_raw": slot.cfg.zero_offset_raw}), 200

@app.route("/api/calibrate/<int:slot_id>", methods=["POST"])
def api_calibrate(slot_id: int):
    data = request.get_json(silent=True) or {}
    known_weight = safe_float(data.get("known_weight"))
    if not known_weight or known_weight <= 0:
        return jsonify({"success": False, "error": "known_weight must be > 0"}), 400

    with inventory.lock:
        slot = inventory.get_slot(slot_id)
        if not slot.has_scale:
            return jsonify({"success": False, "error": "Load cell not detected"}), 400

        raw = slot.nau7802.read_filtered_raw()
        if raw == 0:
            return jsonify({"success": False, "error": "Unable to read load cell"}), 400

        zero = float(slot.cfg.zero_offset_raw or 0.0)
        if zero == 0.0:
            return jsonify({"success": False, "error": "Zero not set. Remove all weight, wait 2-3s, then click Zero Scale."}), 400

        delta = raw - zero
        # NAU7802 raw counts are unitless; some setups yield smaller deltas depending on gain/sample rate.
        # Require a minimum delta, but keep it forgiving for early bring-up.
        if abs(delta) < 200:
            return jsonify({"success": False, "error": "Reading change too small/unstable", "known_weight": known_weight, "raw": raw, "zero": zero, "delta": delta, "hint": "Make sure the known weight is actually on the platform (not touching frame), wait 3-5s, then try again. If delta stays near 0, the NAU7802 is not seeing the bridge signal (wiring/E+/E-/A+/A- or mechanical mount)."}), 400

        factor = delta / float(known_weight)
        if factor == 0:
            return jsonify({"success": False, "error": "Bad calibration factor"}), 400
        # store signed counts-per-gram so direction is preserved
        factor = float(factor)

        slot.cfg.calibration_factor = float(factor)
        slot.cfg.last_calibrated = now_iso()
        inventory._save_config()

        append_history({"ts": now_iso(), "slot": slot_id, "event": "calibrate", "known_weight": known_weight, "factor": slot.cfg.calibration_factor})
        return jsonify({"success": True, "calibration_factor": slot.cfg.calibration_factor, "slot": slot.get_data()}), 200

@app.route("/api/export_csv")
def api_export_csv():
    snap = inventory.snapshot()
    rows = [["Slot","UID","Brand","Type","Color","Gross_g","Tare_g","Net_g","CalStatus","LastCalibrated"]]
    for s in snap.get("slots", []):
        rows.append([
            int(s["slot_id"]) + 1,
            s.get("uid") or "",
            s.get("brand") or "",
            s.get("type") or "",
            s.get("color") or "",
            s.get("gross_weight") or 0,
            s.get("tare") if s.get("tare") is not None else "",
            s.get("weight") or 0,
            s.get("calibration_status") or "",
            s.get("last_calibrated") or "",
        ])

    def esc(v):
        sv = "" if v is None else str(v)
        if any(c in sv for c in [",","\n","\r","\""]):
            sv = sv.replace('"', '""')
            return f'"{sv}"'
        return sv

    csv_text = "\n".join([",".join(esc(c) for c in r) for r in rows])
    buf = io.BytesIO(csv_text.encode("utf-8"))
    return send_file(buf, mimetype="text/csv", as_attachment=True, download_name="inventory_report.csv")

@app.route("/api/export")
def api_export_excel():
    snap = inventory.snapshot()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Inventory"
    ws.append(["Slot","UID","Brand","Type","Color","Gross (g)","Tare (g)","Net (g)","Calibration","Last Calibrated"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDDDDD")
        cell.alignment = Alignment(horizontal="center")

    for s in snap.get("slots", []):
        ws.append([
            int(s["slot_id"]) + 1,
            s.get("uid") or "",
            s.get("brand") or "",
            s.get("type") or "",
            s.get("color") or "",
            s.get("gross_weight") or 0,
            s.get("tare") if s.get("tare") is not None else "",
            s.get("weight") or 0,
            s.get("calibration_status") or "",
            s.get("last_calibrated") or "",
        ])

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(bio, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="inventory_report.xlsx")

@app.route("/api/export_history")
def api_export_history():
    if not os.path.exists(HISTORY_FILE):
        buf = io.BytesIO(b"")
        return send_file(buf, mimetype="text/plain", as_attachment=True, download_name="history.jsonl")
    return send_file(HISTORY_FILE, mimetype="text/plain", as_attachment=True, download_name="history.jsonl")

# ---------------------------
# Entrypoint
# ---------------------------

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "80"))
    app.run(host=host, port=port, debug=False)
