#!/usr/bin/with-contenv bashio
# ==============================================================================
# MyPanel Bridge – entry point
# ==============================================================================

export MYPANEL_LOG_LEVEL=$(bashio::config 'log_level')
export MYPANEL_TOPIC_PREFIX=$(bashio::config 'mqtt_topic_prefix')
export MYPANEL_DISCOVERY_INTERVAL=$(bashio::config 'discovery_interval_seconds')

# Try getting MQTT credentials from Supervisor services API
if bashio::services.available "mqtt"; then
    export MQTT_HOST=$(bashio::services "mqtt" "host")
    export MQTT_PORT=$(bashio::services "mqtt" "port")
    export MQTT_USER=$(bashio::services "mqtt" "username")
    export MQTT_PASS=$(bashio::services "mqtt" "password")
    bashio::log.info "MQTT from services: ${MQTT_HOST}:${MQTT_PORT} user=${MQTT_USER}"
else
    # Fallback: query Supervisor REST API directly
    bashio::log.warning "bashio services unavailable, trying REST API..."
    MQTT_JSON=$(curl -s -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" http://supervisor/services/mqtt)
    export MQTT_HOST=$(echo "$MQTT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin).get('data',{}); print(d.get('host','core-mosquitto'))" 2>/dev/null || echo "core-mosquitto")
    export MQTT_PORT=$(echo "$MQTT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin).get('data',{}); print(d.get('port',1883))" 2>/dev/null || echo "1883")
    export MQTT_USER=$(echo "$MQTT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin).get('data',{}); print(d.get('username',''))" 2>/dev/null || echo "")
    export MQTT_PASS=$(echo "$MQTT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin).get('data',{}); print(d.get('password',''))" 2>/dev/null || echo "")
    bashio::log.info "MQTT from REST: ${MQTT_HOST}:${MQTT_PORT} user=${MQTT_USER}"
fi

export HA_BASE_URL="http://supervisor/core"

bashio::log.info "Starting MyPanel Bridge"
exec python3 /app/main.py
