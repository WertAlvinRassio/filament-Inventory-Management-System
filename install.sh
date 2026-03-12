
#!/bin/bash
set -e

APP_DIR=/opt/filament_inventory
SERVICE_NAME=filament_inventory.service

echo "=== Filament Inventory One-Click Installer ==="

sudo apt update
sudo apt install -y python3 python3-pip
sudo apt install -y hostapd dnsmasq avahi-daemon

sudo mkdir -p $APP_DIR
sudo cp -r ./* $APP_DIR

sudo pip3 install flask openpyxl adafruit-circuitpython-pn532 adafruit-circuitpython-tca9548a adafruit-circuitpython-hdc302x cedargrove-nau7802

# --- Hostname + mDNS (.local) ---
sudo hostnamectl set-hostname filawizard
if ! grep -q "127.0.1.1\s\+filawizard" /etc/hosts; then
  sudo sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tfilawizard/' /etc/hosts || true
fi
sudo systemctl enable avahi-daemon
sudo systemctl restart avahi-daemon

# --- Demo Hotspot (Access Point) mode ---
sudo cp $APP_DIR/scripts/filawizard_ap.service /etc/systemd/system/filawizard_ap.service
sudo systemctl daemon-reload
sudo systemctl enable filawizard_ap.service
# Run once now so the hotspot comes up immediately
sudo bash $APP_DIR/scripts/filawizard_ap_setup.sh
# --- Captive portal auto-redirect (port 80) ---
sudo cp $APP_DIR/scripts/filawizard_portal.service /etc/systemd/system/filawizard_portal.service
sudo systemctl daemon-reload
sudo systemctl enable filawizard_portal.service
sudo systemctl restart filawizard_portal.service


sudo cp $APP_DIR/scripts/$SERVICE_NAME /etc/systemd/system/$SERVICE_NAME
sudo systemctl daemon-reexec
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME
sudo systemctl restart $SERVICE_NAME

echo ""
echo "Installation complete."
echo "Connect to Wi-Fi: FilaWizard_Demo (pass: filawizard123)" 
echo "Open http://192.168.4.1:5000"
