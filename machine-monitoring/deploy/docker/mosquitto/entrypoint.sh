#!/bin/sh
# Regenerate the broker password file from the environment on every start,
# so rotating MQTT_PASSWORD in .env only needs a container restart.
set -eu
umask 077
: "${MQTT_USER:?}" "${MQTT_PASSWORD:?}"
rm -f /mosquitto/data/passwd
touch /mosquitto/data/passwd
mosquitto_passwd -b /mosquitto/data/passwd "$MQTT_USER" "$MQTT_PASSWORD"
chown mosquitto:mosquitto /mosquitto/data /mosquitto/data/passwd
exec mosquitto -c /mosquitto/config/mosquitto.conf
