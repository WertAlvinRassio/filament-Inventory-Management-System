# NFC Write Feature Patch (Brand | Color | Type)

This adds the ability to write filament info to an NFC tag from the web UI when a tag is detected but has no readable info.

## What the UI expects from your API
Your `/api/inventory` response should include per-slot fields:
- `uid`
- `brand`, `color`, `type`
- `nfc_needs_programming` (new, boolean)

The UI will show a **Program NFC** button when:
- a UID is present, AND
- `nfc_needs_programming` is True (or brand/type/color are Unknown/N/A).

---

## 1) Add a flag to your slot classes

### In DemoSlot.__init__ add:
```python
self.nfc_needs_programming = False
```

### In FilamentSlot.__init__ add:
```python
self.nfc_needs_programming = False
```

---

## 2) Set the flag in read_nfc()

### When parsing succeeds:
```python
self.nfc_needs_programming = False
```

### When a UID is present BUT you cannot parse Brand|Color|Type:
```python
self.nfc_needs_programming = True
```

### When no tag is present:
```python
self.nfc_needs_programming = False
```

---

## 3) Include the flag in get_data()

In FilamentSlot.get_data() return dict, add:
```python
'nfc_needs_programming': self.nfc_needs_programming
```

Also add it in DemoSlot.get_data().

---

## 4) Add a write method to FilamentSlot

Add inside class FilamentSlot:

```python
def write_nfc(self, brand: str, color: str, filament_type: str) -> bool:
    """Write Brand|Color|Type into NTAG blocks 4-7 (64 bytes)."""
    if not self.nfc or not self.nfc_available:
        return False
    if not self.uid:
        return False

    payload = f"{brand.strip()}|{color.strip()}|{filament_type.strip()}".encode("utf-8")
    payload = payload[:64]
    payload = payload + b"\x00" * (64 - len(payload))

    try:
        if not self.mux.select_channel(self.channel):
            return False

        for i in range(4):
            chunk = payload[i*16:(i+1)*16]
            ok = self.nfc.ntag2xx_write_block(4 + i, bytearray(chunk))
            if not ok:
                return False

        self.filament_brand = brand.strip()
        self.filament_color = color.strip()
        self.filament_type = filament_type.strip()
        self.nfc_needs_programming = False
        self.is_active = True
        return True
    except Exception as e:
        print(f"Slot {self.slot_id} NFC write error: {e}")
        return False
    finally:
        try:
            self.mux.disable_all()
        except:
            pass
```

---

## 5) Add a write method to DemoSlot (optional)

```python
def write_nfc(self, brand, color, filament_type):
    self.filament_brand = brand
    self.filament_color = color
    self.filament_type = filament_type
    self.nfc_needs_programming = False
    return True
```

---

## 6) Add the Flask API route

Add near your other routes:

```python
@app.route('/api/program_nfc/<int:slot_id>', methods=['POST'])
def api_program_nfc(slot_id):
    if 0 <= slot_id < len(inventory.slots):
        slot = inventory.slots[slot_id]
        data = request.json or {}
        brand = (data.get('brand') or '').strip()
        color = (data.get('color') or '').strip()
        filament_type = (data.get('type') or '').strip()

        if not brand or not color or not filament_type:
            return jsonify({'success': False, 'error': 'Brand, Color, and Type are required'}), 400

        if not getattr(slot, 'uid', None):
            return jsonify({'success': False, 'error': 'No NFC tag detected on this slot'}), 400

        ok = slot.write_nfc(brand, color, filament_type)
        if ok:
            try:
                inventory.save_config()
            except:
                pass
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'Failed to write to NFC tag'}), 500

    return jsonify({'success': False, 'error': 'Invalid slot'}), 400
```

---

## 7) Replace templates/index.html
Use the `templates/index.html` included in this package (it already includes the **Program NFC** modal and button).
