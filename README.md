# Home-Assistant-Aldes-EasyHome-PureAir-Connect-MQTT-Bridge
Home Assistant Add-on for local control of Aldes EasyHome PureAir Connect, by intercepting MQTT/TLS to Azure IoT Hub

Makes the Connect Box believe it is communicating with Aldes Azure IoT Hub cloud, publishes its measurements to Home Assistant via MQTT (automatic discovery), and sends mode commands to it.

## Installation
- ## Add the repository
Note: This is a repository for add-ons (containers), not HACS. Do not add it to HACS (which is for integrations, cards, and themes).

- ## In HAOS:

  - **Select** Settings → Add-ons → Stores (at the bottom) → Add a repository
  - **Enter** URL: https://github.com/DKFR67/Home-Assistant-Aldes-EasyHome-PureAir-Connect-MQTT-Bridge
  - **Tap** Create

- ## Options
  - **device_mac** (required): MAC address of the Wi-Fi module, e.g., '98D8632BD2F1'.
  - **device_suffix** : '_EASYH' for an EasyHome PureAir Ventilation unit.
  - **mqtt_host / mqtt_user / mqtt_password** : Leave blank to automatically use HA's Mosquitto.
  - **topic_prefix** : Topic prefix (default 'aldes/vmc').

## Network Prerequisites
1. The DNS server serving the VMC must resolve 'aldesiotsuite.azure-devices.net' to the IP address of this Home Assistant instance, in my setup, I use Adguardhome.
2. Port **8883** on the machine must be open (see the Network tab in the Mosquitto add-on).
3. Only one bridge at a time: stop other one before starting this one.

## Credits
Inspired by [djo1338/aldes-mqtt-bridge](https://github.com/djo1338/aldes-mqtt-bridge) and the work of [aalmazanarbs/hassio_aldes](https://github.com/aalmazanarbs/hassio_aldes)
Adapted to my ventilation unit model and packaged as an add-on that can be used directly in Home Assistant.
