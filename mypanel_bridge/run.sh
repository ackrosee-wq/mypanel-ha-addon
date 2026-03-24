#!/usr/bin/with-contenv bashio
# ==============================================================================
# MyPanel Bridge – entry point
# Reads add-on options via bashio helpers and exports them as environment
# variables consumed by the Python application.
# ==============================================================================

declare LOG_LEVEL
declare MQTT_TOPIC_PREFIX
declare DISCOVERY_INTERVAL

LOG_LEVEL=$(bashio::config 'log_level')
MQTT_TOPIC_PREFIX=$(bashio::config 'mqtt_topic_prefix')
DISCOVERY_INTERVAL=$(bashio::config 'discovery_interval_seconds')

export MYPANEL_LOG_LEVEL="${LOG_LEVEL}"
export MYPANEL_TOPIC_PREFIX="${MQTT_TOPIC_PREFIX}"
export MYPANEL_DISCOVERY_INTERVAL="${DISCOVERY_INTERVAL}"

# MQTT connection details are provided by the Supervisor MQTT service.
if bashio::services.available "mqtt"; then
    export MQTT_HOST=$(bashio::services "mqtt" "host")
    export MQTT_PORT=$(bashio::services "mqtt" "port")
    export MQTT_USER=$(bashio::services "mqtt" "username")
    export MQTT_PASS=$(bashio::services "mqtt" "password")
    bashio::log.info "MQTT service found at ${MQTT_HOST}:${MQTT_PORT}"
else
    bashio::log.warning "MQTT service not available – will retry from Python"
fi

# The Supervisor injects SUPERVISOR_TOKEN automatically for API calls.
# HA_TOKEN is also available when homeassistant_api is granted.
export HA_BASE_URL="http://supervisor/core"
export SUPERVISOR_API="http://supervisor"

bashio::log.info "Starting MyPanel Bridge (log_level=${LOG_LEVEL})"
exec python3 /app/main.py
