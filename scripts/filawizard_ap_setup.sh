#!/bin/bash
set -e

SSID="FilaWizard_Demo"
PASSPHRASE="filawizard123"
AP_IP="192.168.4.1"

echo "[AP] Installing required packages..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y hostapd dnsmasq

systemctl unmask hostapd || true
systemctl enable hostapd
systemctl enable dnsmasq

echo "[AP] Configure wlan0 static IP via dhcpcd.conf..."
DHCPCD=/etc/dhcpcd.conf
if ! grep -q "## FILAWIZARD_DEMO_AP_START" "$DHCPCD"; then
  cat >> "$DHCPCD" <<EOF

## FILAWIZARD_DEMO_AP_START
interface wlan0
static ip_address=${AP_IP}/24
nohook wpa_supplicant
## FILAWIZARD_DEMO_AP_END
EOF
fi

echo "[AP] Configure dnsmasq..."
if [ -f /etc/dnsmasq.conf ] && [ ! -f /etc/dnsmasq.conf.filawizard.bak ]; then
  cp /etc/dnsmasq.conf /etc/dnsmasq.conf.filawizard.bak
fi
cat > /etc/dnsmasq.conf <<EOF
interface=wlan0
dhcp-range=192.168.4.10,192.168.4.50,255.255.255.0,24h
dhcp-option=3,192.168.4.1
dhcp-option=6,192.168.4.1
address=/#/192.168.4.1
domain-needed
bogus-priv
EOF

echo "[AP] Configure hostapd..."
mkdir -p /etc/hostapd
cat > /etc/hostapd/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid=${SSID}
hw_mode=g
channel=7
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=${PASSPHRASE}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF

echo "[AP] Point hostapd default to our config..."
DEFAULT_HOSTAPD=/etc/default/hostapd
if [ -f "$DEFAULT_HOSTAPD" ]; then
  if grep -q '^DAEMON_CONF=' "$DEFAULT_HOSTAPD"; then
    sed -i 's|^DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' "$DEFAULT_HOSTAPD"
  else
    echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' >> "$DEFAULT_HOSTAPD"
  fi
else
  echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' > "$DEFAULT_HOSTAPD"
fi

echo "[AP] Restart services..."
systemctl restart dhcpcd || true
systemctl restart dnsmasq
systemctl restart hostapd

echo "[AP] Demo hotspot enabled:"
echo "    SSID: ${SSID}"
echo "    Pass: ${PASSPHRASE}"
echo "    UI  : http://${AP_IP}:5000"
