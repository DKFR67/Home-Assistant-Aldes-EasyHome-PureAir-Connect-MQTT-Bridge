# Aldes VMC Local Bridge

Fait croire a la Connect Box qu'elle parle au cloud Azure IoT Hub d'Aldes, publie ses mesures
dans Home Assistant par MQTT (decouverte automatique) et lui envoie les commandes de mode.

## Options
- **device_mac** (obligatoire) : MAC du module Wi-Fi, ex. `98D8632BD1F2`.
- **device_suffix** : `_EASYH` pour une EasyHome.
- **mqtt_host / mqtt_user / mqtt_password** : laisser vides pour utiliser automatiquement le Mosquitto de HA.
- **topic_prefix** : prefixe des topics (defaut `aldes/vmc`).

## Prerequis reseau
1. Le DNS qui sert la VMC doit resoudre `aldesiotsuite.azure-devices.net` vers l'IP de ce Home Assistant.
2. Le port **8883** de la machine doit etre libre (voir l'onglet Reseau de l'add-on Mosquitto).
3. Un seul pont a la fois : arreter l'ancien avant de demarrer celui-ci.
