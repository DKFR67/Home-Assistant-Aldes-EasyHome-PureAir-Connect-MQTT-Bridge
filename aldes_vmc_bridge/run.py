#!/usr/bin/env python3
"""Point d'entree de l'add-on : options HA -> variables d'environnement -> fake_iothub.py."""
import json, os, runpy, sys, urllib.request

OPTIONS_FILE = os.getenv("OPTIONS_FILE", "/data/options.json")
APP_FILE = os.getenv("APP_FILE", "/app/fake_iothub.py")


def supervisor_mqtt():
    """Identifiants du broker Mosquitto fournis par le Supervisor (service 'mqtt')."""
    token = os.getenv("SUPERVISOR_TOKEN")
    if not token:
        return {}
    req = urllib.request.Request(
        "http://supervisor/services/mqtt",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("data") or {}
    except Exception as e:
        print(f"[addon] service MQTT du Supervisor indisponible: {e}", flush=True)
        return {}


def main():
    with open(OPTIONS_FILE, encoding="utf-8") as f:
        opt = json.load(f)

    mac = (opt.get("device_mac") or "").strip()
    if not mac:
        sys.exit("[addon] FATAL: l'option 'device_mac' est obligatoire (MAC du module Wi-Fi, 12 caracteres hexa).")

    host = (opt.get("mqtt_host") or "").strip()
    port = opt.get("mqtt_port") or 1883
    user = opt.get("mqtt_user") or ""
    pwd = opt.get("mqtt_password") or ""
    if not host:  # mode automatique : on demande au Supervisor
        svc = supervisor_mqtt()
        host = svc.get("host") or "core-mosquitto"
        port = svc.get("port") or port
        user = svc.get("username") or user
        pwd = svc.get("password") or pwd

    suffix = opt.get("device_suffix", "_EASYH")
    os.environ.update({
        "DEVICE_MAC": mac,
        "DEVICE_SUFFIX": suffix,
        "DEVICE_NAME": opt.get("device_name") or "Aldes VMC",
        "DEVICE_MODEL": opt.get("device_model") or "EasyHome PureAir",
        "BRIDGE_TOPIC_PREFIX": opt.get("topic_prefix") or "aldes/vmc",
        "UPSTREAM_MQTT_HOST": host,
        "UPSTREAM_MQTT_PORT": str(port),
        "UPSTREAM_MQTT_USER": user,
        "UPSTREAM_MQTT_PASS": pwd,
        "CERT_DIR": os.getenv("CERT_DIR", "/data"),   # persiste entre redemarrages
        "LOG_FILE": os.getenv("LOG_FILE", "/dev/null"),  # les logs vont dans l'onglet Journal
    })
    print(f"[addon] MQTT {host}:{port} (auth: {'oui' if user else 'non'}), "
          f"device {mac.upper().replace(':', '').replace('-', '')}{suffix}", flush=True)
    runpy.run_path(APP_FILE, run_name="__main__")


if __name__ == "__main__":
    main()
