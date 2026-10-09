"""
Fake Azure IoT Hub MQTT/TLS broker for Aldes Connect Box.

Architecture:
  Connect Box --(MQTT/TLS)--> [this fake broker] --(MQTT)--> [user's Mosquitto] --> Home Assistant

The box thinks it's talking to aldesiotsuite.azure-devices.net (DNS rewritten via Pi-hole).
This broker:
  - accepts the TLS connection with a self-signed cert (CN=aldesiotsuite.azure-devices.net)
  - parses MQTT packets (CONNECT auth, SUBSCRIBE topics, PUBLISH telemetry)
  - logs everything to a file
  - bridges captured PUBLISHes to the user's existing Mosquitto under a clean topic prefix
  - listens on the user's Mosquitto for command injections at <prefix>/cmd/+ and forwards to the box

Env vars:
  CERT_DIR               where to store the generated cert/key (default: /data)
  UPSTREAM_MQTT_HOST     bridge destination (default: 127.0.0.1)
  DEVICE_MAC             REQUIRED. The HF-LPB100 module MAC (12 hex chars, ':' or '-' OK)
  DEVICE_NAME            HA device name (default: "Aldes VMC")
  DEVICE_MODEL           HA device model (default: "Dee Fly Cube")
  UPSTREAM_MQTT_PORT     bridge port (default: 1883)
  UPSTREAM_MQTT_USER     optional auth
  UPSTREAM_MQTT_PASS     optional auth
  BRIDGE_TOPIC_PREFIX    topic prefix for republished messages (default: aldes/vmc)
  LOG_FILE               additional log file (default: /logs/mitm.log)
"""
import json, os, socket, ssl, struct, sys, threading, time, queue
from datetime import datetime, timedelta
from pathlib import Path

CERT_DIR = Path(os.getenv("CERT_DIR", "/data"))
CERT_DIR.mkdir(parents=True, exist_ok=True)
CERT_FILE = CERT_DIR / "fake_azure.crt"
KEY_FILE = CERT_DIR / "fake_azure.key"
SERVER_CN = "aldesiotsuite.azure-devices.net"

UPSTREAM_HOST = os.getenv("UPSTREAM_MQTT_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.getenv("UPSTREAM_MQTT_PORT", "1883"))
UPSTREAM_USER = os.getenv("UPSTREAM_MQTT_USER", "") or None
UPSTREAM_PASS = os.getenv("UPSTREAM_MQTT_PASS", "") or None
PREFIX = os.getenv("BRIDGE_TOPIC_PREFIX", "aldes/vmc")
LOG_FILE = os.getenv("LOG_FILE", "/logs/mitm.log")

# Device identity — DEVICE_MAC is REQUIRED
_DEVICE_MAC_RAW = os.getenv("DEVICE_MAC", "").upper().replace(":", "").replace("-", "")
if not _DEVICE_MAC_RAW or len(_DEVICE_MAC_RAW) != 12:
    sys.exit("FATAL: DEVICE_MAC env var is required (12 hex chars, with or without ':')")
_DEVICE_SUFFIX = os.getenv("DEVICE_SUFFIX", "_AIR")  # Aldes appends "_AIR" for ventilation
_DEVICE_ID_DEFAULT = _DEVICE_MAC_RAW + _DEVICE_SUFFIX
DEVICE_NAME = os.getenv("DEVICE_NAME", "Aldes VMC")
DEVICE_MODEL = os.getenv("DEVICE_MODEL", "Dee Fly Cube")
HA_DISCOVERY_PREFIX = os.getenv("HA_DISCOVERY_PREFIX", "homeassistant")

HOST = "0.0.0.0"
PORT = 8883


# ---------- logging ----------

_log_lock = threading.Lock()
_log_fp = None
try:
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    _log_fp = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
except Exception:
    pass


def log(msg):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        if _log_fp:
            try:
                _log_fp.write(line + "\n")
            except Exception:
                pass


# ---------- self-signed cert ----------

def gen_cert():
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    log(f"generating self-signed cert for {SERVER_CN}...")
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "FR"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FakeAzure"),
        x509.NameAttribute(NameOID.COMMON_NAME, SERVER_CN),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow() - timedelta(days=1))
        .not_valid_after(datetime.utcnow() + timedelta(days=365 * 10))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName(SERVER_CN),
                x509.DNSName("*.azure-devices.net"),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    KEY_FILE.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    log(f"cert written: {CERT_FILE}")


# ---------- minimal MQTT 3.1.1 codec ----------

CONNECT, CONNACK, PUBLISH, PUBACK, SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = (
    1, 2, 3, 4, 8, 9, 12, 13, 14
)


def read_remaining_length(s):
    multiplier = 1
    value = 0
    for _ in range(4):
        b = s.recv(1)
        if not b:
            return None
        digit = b[0]
        value += (digit & 0x7F) * multiplier
        if (digit & 0x80) == 0:
            return value
        multiplier *= 128
    return value


def encode_remaining_length(length):
    out = b""
    while True:
        digit = length & 0x7F
        length >>= 7
        if length > 0:
            digit |= 0x80
        out += bytes([digit])
        if length == 0:
            break
    return out


def parse_string(buf, off):
    n = struct.unpack_from(">H", buf, off)[0]
    return buf[off + 2: off + 2 + n].decode("utf-8", errors="replace"), off + 2 + n


def encode_string(s):
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


# ---------- upstream bridge to user's Mosquitto ----------

class UpstreamBridge:
    def __init__(self):
        import paho.mqtt.client as mqtt
        self.client = mqtt.Client(client_id="aldes-mitm-bridge", clean_session=True)
        if UPSTREAM_USER:
            self.client.username_pw_set(UPSTREAM_USER, UPSTREAM_PASS)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.cmd_callbacks = []  # list of fn(topic, payload) called when user injects a cmd
        self._connect()

    def _connect(self):
        try:
            self.client.connect(UPSTREAM_HOST, UPSTREAM_PORT, keepalive=60)
            self.client.loop_start()
            log(f"upstream MQTT connected to {UPSTREAM_HOST}:{UPSTREAM_PORT}")
        except Exception as e:
            log(f"upstream MQTT FAILED: {e}")

    def _on_connect(self, client, userdata, flags, rc):
        log(f"upstream on_connect rc={rc}")
        # Subscribe to command-injection topic so HA can publish e.g. aldes/vmc/cmd/changeMode
        client.subscribe(f"{PREFIX}/cmd/#")
        # Publish HA MQTT discovery for the VMC entities
        publish_ha_discovery(client)

    def _on_message(self, client, userdata, msg):
        log(f"upstream RX cmd topic={msg.topic} payload={msg.payload!r}")
        for cb in self.cmd_callbacks:
            try:
                cb(msg.topic, msg.payload)
            except Exception as e:
                log(f"cmd callback err: {e}")

    def publish(self, subtopic, payload, qos=0, retain=False):
        topic = f"{PREFIX}/{subtopic}"
        try:
            self.client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as e:
            log(f"upstream publish err: {e}")


bridge = None


# ---------- Home Assistant MQTT Discovery ----------

DEVICE_ID = os.getenv("DEVICE_ID", "") or _DEVICE_ID_DEFAULT
DEVICE_MAC = _DEVICE_MAC_RAW
HA_DISCO_PREFIX = HA_DISCOVERY_PREFIX
EVENTS_TOPIC = f"{PREFIX}/raw/devices/{DEVICE_ID}/messages/events/"
CMD_TOPIC = f"{PREFIX}/cmd/devices/{DEVICE_ID}/messages/devicebound"

DEVICE_BLOCK = {
    "identifiers": [f"aldes_vmc_{DEVICE_MAC}"],
    "name": DEVICE_NAME,
    "manufacturer": "Aldes",
    "model": DEVICE_MODEL,
    "sw_version": "Connect Box (HF-LPB100)",
}

# Each tuple: (object_id, friendly_name, payload_key, dict_extra)
def _t10(k):
    return "{{ (value_json." + k + " | float / 10) | round(1) if value_json." + k + " is defined else none }}"


SENSORS = [
    ("kitchen_temperature", "Température cuisine", "TmpCu", {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement", "value_template": _t10("TmpCu")}),
    ("kitchen_humidity", "Humidité cuisine", "HrCu", {"device_class": "humidity", "unit_of_measurement": "%", "state_class": "measurement"}),
    ("bath1_temperature", "Température salle de bain", "TmpBa1", {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement", "value_template": _t10("TmpBa1")}),
    ("bath1_humidity", "Humidité salle de bain", "HrBa1", {"device_class": "humidity", "unit_of_measurement": "%", "state_class": "measurement"}),
    ("bath2_temperature", "Température toilettes", "TmpBa2", {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement", "value_template": _t10("TmpBa2")}),
    ("bath2_humidity", "Humidité toilettes", "HrBa2", {"device_class": "humidity", "unit_of_measurement": "%", "state_class": "measurement"}),
    ("co2", "CO₂", "CO2", {"device_class": "carbon_dioxide", "unit_of_measurement": "ppm", "state_class": "measurement"}),
    ("mode_raw", "Mode (brut)", "ConVe", {"icon": "mdi:fan"}),
    ("pwm_real", "Vitesse réelle", "PwmReal", {"entity_category": "diagnostic", "state_class": "measurement"}),
    ("pwm_qai", "Vitesse demandée (capteurs)", "PwmQai", {"entity_category": "diagnostic", "state_class": "measurement"}),
    ("var_hr", "Variation humidité", "VarHR", {"unit_of_measurement": "%", "icon": "mdi:cloud-percent", "state_class": "measurement"}),
    ("fw_wifi", "Firmware WiFi", "Vers_W", {"icon": "mdi:chip", "entity_category": "diagnostic"}),
    ("fw_uc", "Firmware MCU", "Vers_UC", {"icon": "mdi:chip", "entity_category": "diagnostic"}),
]

MODES = {
    "V": "Quotidien",
    "W": "Vacances",
    "X": "Invités",
    "Y": "Boost",
    "Z": "Programme",
}


def publish_ha_discovery(client):
    """Publish HA MQTT Discovery configs for all VMC entities. Called once on bridge connect."""
    log("publishing HA discovery...")
    base_id = f"aldes_vmc_{DEVICE_MAC}"

    # Sensors
    for obj_id, name, key, extra in SENSORS:
        cfg = {
            "name": name,
            "unique_id": f"{base_id}_{obj_id}",
            "state_topic": EVENTS_TOPIC,
            "value_template": f"{{{{ value_json.{key} | default(none) }}}}",
            "device": DEVICE_BLOCK,
            **extra,
        }
        topic = f"{HA_DISCO_PREFIX}/sensor/{base_id}/{obj_id}/config"
        client.publish(topic, json.dumps(cfg), qos=1, retain=True)

    # Mode select : 4 ventilation modes + Programme. Vacances auto-resolves now / now+1d
    # via a special "macro" topic the broker translates server-side (date math is hard in Jinja).
    mode_select_cfg = {
        "name": "Mode",
        "unique_id": f"{base_id}_mode",
        "state_topic": EVENTS_TOPIC,
        "value_template": "{% set m = {'W':'Holiday','V':'Daily','Y':'Boost','X':'Guest'} %}{{ m.get(value_json.ConVe, 'Unknown') }}",
        "command_topic": f"{PREFIX}/cmd/macro/_select",
        "command_template": "{{ value }}",
        "options": ["Holiday", "Daily", "Guest", "Boost"],
        "icon": "mdi:fan-clock",
        "device": DEVICE_BLOCK,
    }
    client.publish(
        f"{HA_DISCO_PREFIX}/select/{base_id}/mode/config",
        json.dumps(mode_select_cfg), qos=1, retain=True,
    )

    # Quick-action buttons via macro topics (broker translates server-side)
    macro_topic = f"{PREFIX}/cmd/macro"
    macros = [
        ("quotidien", "Daily", "mdi:fan-speed-2"),
        ("invites",   "Guest",   "mdi:fan-speed-3"),
        ("boost",     "Boost",     "mdi:fan-plus"),
        ("vacances",  "Holiday",  "mdi:bag-suitcase"),
    ]
    for _old in ("vacances1j", "vacances7j", "vacances_stop", "programme"):
        client.publish(f"{HA_DISCO_PREFIX}/button/{base_id}/btn_{_old}/config", "", qos=1, retain=True)
    for key, name, icon in macros:
        cfg = {
            "name": name,
            "unique_id": f"{base_id}_btn_{key}",
            "command_topic": f"{macro_topic}/{key}",
            "payload_press": "1",
            "icon": icon,
            "device": DEVICE_BLOCK,
        }
        client.publish(
            f"{HA_DISCO_PREFIX}/button/{base_id}/btn_{key}/config",
            json.dumps(cfg), qos=1, retain=True,
        )

    log(f"HA discovery: {len(SENSORS)} sensors + 1 select + {len(macros)} buttons")


# ---------- per-client MQTT handler ----------

# Track connected box sessions so we can inject commands back to them
sessions = {}  # client_id -> {sock, lock, subscriptions, last_pkt_id}


def send_publish_to_box(client_id, topic, payload, qos=1):
    """Inject a PUBLISH back to a connected device (QoS 1 with packet id)."""
    sess = sessions.get(client_id)
    if not sess:
        log(f"inject: client {client_id} not connected")
        return False
    if isinstance(payload, str):
        payload = payload.encode()
    try:
        with sess["lock"]:
            body = encode_string(topic)
            if qos > 0:
                sess["pkt_id"] = (sess.get("pkt_id", 0) % 65535) + 1
                body += struct.pack(">H", sess["pkt_id"])
            body += payload
            pkt = bytes([0x30 | (qos << 1)]) + encode_remaining_length(len(body)) + body
            sess["sock"].sendall(pkt)
        log(f"inject: sent to {client_id} topic={topic} qos={qos} ({len(payload)} bytes)")
        return True
    except Exception as e:
        log(f"inject err: {e}")
        return False


def handle_client(tls_sock, addr):
    log(f"NEW CONNECTION from {addr}")
    client_id = None
    sock_lock = threading.Lock()
    subs = []
    try:
        while True:
            head = tls_sock.recv(1)
            if not head:
                break
            ptype = (head[0] >> 4) & 0xF
            flags = head[0] & 0xF
            length = read_remaining_length(tls_sock)
            if length is None:
                break
            payload = b""
            while len(payload) < length:
                chunk = tls_sock.recv(length - len(payload))
                if not chunk:
                    break
                payload += chunk

            if ptype == CONNECT:
                proto, off = parse_string(payload, 0)
                level = payload[off]; off += 1
                cflags = payload[off]; off += 1
                keepalive = struct.unpack_from(">H", payload, off)[0]; off += 2
                cid, off = parse_string(payload, off)
                client_id = cid
                username = pwd = None
                if cflags & 0x80:
                    username, off = parse_string(payload, off)
                if cflags & 0x40:
                    pwlen = struct.unpack_from(">H", payload, off)[0]
                    pwd = payload[off+2:off+2+pwlen]
                    off += 2 + pwlen
                log(f"{addr} CONNECT proto={proto} level={level} keepalive={keepalive}")
                log(f"{addr}   client_id = {cid}")
                log(f"{addr}   username  = {username}")
                if pwd:
                    log(f"{addr}   password  = {pwd[:60]!r}{'...' if len(pwd)>60 else ''}")
                # Register session
                sessions[cid] = {"sock": tls_sock, "lock": sock_lock, "subs": subs}
                # Reply CONNACK accepted (session_present=0, return_code=0)
                tls_sock.sendall(bytes([0x20, 0x02, 0x00, 0x00]))
                # Bridge metadata
                if bridge:
                    bridge.publish(f"_meta/connect", f"{cid} u={username}")

            elif ptype == PUBLISH:
                qos = (flags >> 1) & 0x3
                topic, off = parse_string(payload, 0)
                pkt_id = None
                if qos > 0:
                    pkt_id = struct.unpack_from(">H", payload, off)[0]; off += 2
                msg = payload[off:]
                preview = msg[:300]
                log(f"{addr} PUBLISH qos={qos} topic={topic} ({len(msg)}b)")
                log(f"{addr}   payload = {preview!r}{'...' if len(msg)>300 else ''}")
                if qos == 1 and pkt_id is not None:
                    tls_sock.sendall(bytes([0x40, 0x02]) + struct.pack(">H", pkt_id))
                # Bridge to upstream — retain=true so HA has the last value after reloads
                if bridge:
                    bridge.publish(f"raw/{topic.replace('$', '_').replace('#', '_')}", msg, retain=True)

            elif ptype == SUBSCRIBE:
                pkt_id = struct.unpack_from(">H", payload, 0)[0]
                off = 2
                topics = []
                while off < len(payload):
                    t, off = parse_string(payload, off)
                    qos = payload[off]; off += 1
                    topics.append((t, qos))
                    subs.append(t)
                log(f"{addr} SUBSCRIBE pkt_id={pkt_id}")
                for t, q in topics:
                    log(f"{addr}   topic = {t!r}  qos={q}")
                resp = bytes([0x90])
                body = struct.pack(">H", pkt_id) + bytes([min(q, 1) for _, q in topics])
                resp += encode_remaining_length(len(body)) + body
                tls_sock.sendall(resp)

            elif ptype == PINGREQ:
                tls_sock.sendall(bytes([0xD0, 0x00]))

            elif ptype == PUBACK:
                log(f"{addr} PUBACK pkt_id={struct.unpack('>H', payload[:2])[0]}")

            elif ptype == DISCONNECT:
                log(f"{addr} DISCONNECT")
                break
            else:
                log(f"{addr} unknown packet type={ptype} flags={flags} len={length}")
                if payload:
                    log(f"{addr}   data = {payload[:60].hex(' ')}")
    except ssl.SSLError as e:
        log(f"{addr} TLS error: {e}")
    except Exception as e:
        log(f"{addr} error: {type(e).__name__}: {e}")
    finally:
        try:
            tls_sock.close()
        except Exception:
            pass
        if client_id and client_id in sessions:
            del sessions[client_id]
        log(f"{addr} closed (client_id={client_id})")


def _macro_payload(macro):
    """Translate a high-level macro name to the proper Aldes JSON-RPC payload."""
    from datetime import datetime, timedelta, timezone
    m = macro.strip().lower().replace("é", "e").replace("è", "e")
    m = {"daily": "quotidien", "guest": "invites", "holiday": "vacances"}.get(m, m)
    if m in ("quotidien", "v", "v2"):
        return '{"method":"changeMode","params":["V"]}'
    if m in ("invites", "guests", "v3", "kitchen", "cuisine", "x"):
        return '{"method":"changeMode","params":["X"]}'
    if m in ("boost", "v4", "max", "y"):
        return '{"method":"changeMode","params":["Y"]}'
    if m in ("programme", "z"):
        return '{"method":"changeMode","params":["Z"]}'
    if m in ("override_on", "cmo_on"):
        return '{"method":"changeCMO","params":[1]}'
    if m in ("override_off", "cmo_off"):
        return '{"method":"changeCMO","params":[0]}'
    if m == "vacances_stop":
        # Cancelling holidays: send epoch placeholder per APK
        epoch = "00010101000000Z"
        return '{"method":"changeMode","params":["W' + epoch + epoch + '"]}'
    if m.startswith("vacances") or m == "w":
        # vacances / vacances1j / vacances7j / vacances14j → now → now + N days (default 1)
        days = 1
        for n in (14, 7, 3, 1):
            if m.endswith(f"{n}j") or m.endswith(f"{n}d"):
                days = n; break
        fmt = "%Y%m%d%H%M%SZ"
        start = datetime.now(timezone.utc).strftime(fmt)
        end = (datetime.now(timezone.utc) + timedelta(days=days)).strftime(fmt)
        return '{"method":"changeMode","params":["W' + start + end + '"]}'
    return None


def cmd_callback(topic, payload):
    """Forward injection commands to the box.
    Two routes:
      aldes/vmc/cmd/macro/<name>   -> macro translation (V1/V3/Boost/Vacances1J/...)
      aldes/vmc/cmd/<azure_topic>  -> raw forward (e.g. devices/.../messages/devicebound)
    """
    if not topic.startswith(f"{PREFIX}/cmd/"):
        return
    relative = topic[len(f"{PREFIX}/cmd/"):]

    # Macro mode:
    #   aldes/vmc/cmd/macro/<name>           -> use <name>
    #   aldes/vmc/cmd/macro/_select payload  -> use payload as macro name (HA select)
    if relative.startswith("macro/"):
        macro_name = relative[len("macro/"):]
        if macro_name == "_select":
            try:
                macro_name = payload.decode("utf-8").strip()
            except Exception:
                macro_name = ""
        translated = _macro_payload(macro_name)
        if not translated:
            log(f"macro '{macro_name}' unknown, dropped")
            return
        relative = f"devices/{DEVICE_ID}/messages/devicebound/%24.to=%2Fdevices%2F{DEVICE_ID}%2Fmessages%2FdeviceBound"
        _d = json.loads(translated)
        _d = {"id": int(time.time()) % 100000, "jsonrpc": "2.0", **_d}
        payload = json.dumps(_d, separators=(",", ":")).encode()
        log(f"envelope -> {payload.decode()}")
        log(f"macro '{macro_name}' -> {translated}")

    # Send to first connected session (typical: 1 box)
    if not sessions:
        log(f"cmd: no active box session, dropped {topic}")
        return
    cid = next(iter(sessions.keys()))
    send_publish_to_box(cid, relative, payload)


def main():
    global bridge
    gen_cert()
    try:
        bridge = UpstreamBridge()
        bridge.cmd_callbacks.append(cmd_callback)
    except Exception as e:
        log(f"bridge init failed: {e} (will run without bridge)")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # HF-LPB100 firmware speaks legacy TLS (small set of older ciphers).
    # Relax everything so we can negotiate.
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1
    except (AttributeError, ValueError):
        pass
    try:
        ctx.set_ciphers("ALL:@SECLEVEL=0")
    except ssl.SSLError as e:
        log(f"set_ciphers err: {e}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, PORT))
    sock.listen(8)
    log(f"fake Azure IoT Hub listening on {HOST}:{PORT} (TLS, CN={SERVER_CN})")
    log(f"bridging captured PUBLISHes to mqtt://{UPSTREAM_HOST}:{UPSTREAM_PORT} prefix={PREFIX}")

    while True:
        client, addr = sock.accept()
        try:
            tls = ctx.wrap_socket(client, server_side=True)
            threading.Thread(target=handle_client, args=(tls, addr), daemon=True).start()
        except ssl.SSLError as e:
            log(f"{addr} TLS handshake FAILED: {e}")
            try: client.close()
            except: pass


if __name__ == "__main__":
    main()
