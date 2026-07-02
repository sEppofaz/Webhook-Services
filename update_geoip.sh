#!/bin/bash
set -e
source /etc/pka/secrets.env
curl -fsSL "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-City&license_key=&suffix=tar.gz" -o /tmp/geoip_update.tar.gz
tar -xzf /tmp/geoip_update.tar.gz -C /tmp/
find /tmp -name 'GeoLite2-City.mmdb' -exec mv {} /opt/rename-webhook/GeoLite2-City.mmdb \;
rm -f /tmp/geoip_update.tar.gz
rm -rf /tmp/GeoLite2-City_*
echo "$(date): GeoLite2-City.mmdb aktualisiert"
