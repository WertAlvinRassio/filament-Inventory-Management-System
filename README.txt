
FILAMENT INVENTORY — ONE CLICK INSTALL (v3)

\1✔ FILA-WIZARD aesthetic refresh (red/black UI)
✔ Auto-start on Raspberry Pi boot
✔ Full Web UI
✔ Search + Sort
✔ CSV Inventory Export
✔ Per-slot history log
✔ Conflict detection workflow
✔ NFC Programming (Brand | Color | Type)
✔ NFC Rewrite mode (arm button + confirm warning)

INSTALL STEPS:
1. Copy folder to Raspberry Pi
2. Paste your full Python app into app.py
3. Ensure templates/index.html is present (included)
4. Run:
   chmod +x install.sh
   ./install.sh

IMPORTANT:
To enable NFC programming, follow instructions in:
PATCH_NFC_WRITE.md

Access after install:
http://raspberrypi.local:5000

UI: Rewrite confirm modal shows current tag contents (brand/color/type) before overwrite.

UI: Added Graphical Slots view (card layout) with green/yellow/red fill based on weight.

UI: Slot cards are clickable; opens an actions popup (tare, write/rewrite NFC, history).

Tare: Stored on NFC tags (per-roll). See PATCH_NFC_TARE.md

UI: If NFC tag is blank but weight is detected, prompts NEW roll flow and guides user to program tag with tare.

Calibration: UI shows status per slot; status becomes Not calibrated if last calibration is older than 365 days.


DEMO HOTSPOT MODE (always on at boot)
- SSID: FilaWizard_Demo
- Password: filawizard123
- Open UI: http://192.168.4.1:5000


CAPTIVE PORTAL AUTO-REDIRECT
- Connect to SSID: FilaWizard_Demo (pass: filawizard123)
- Most phones will auto-open the portal.
- If not, open: http://filawizard.local:5000 (or http://192.168.4.1:5000)
