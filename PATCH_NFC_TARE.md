# Patch: Store Tare on NFC Tag (instead of per-slot)

This patch changes tare handling so it is stored on the **NFC tag** (per-roll), not saved per-slot.

## UI changes already included
- "Set Tare" writes tare to the tag by calling:
  `POST /api/program_nfc/<slot_id>` with `{brand,color,type,tare}`
- "Write NFC" and "Rewrite NFC" include an optional **Tare** field.
- Rewrite confirmation shows **current** and **new** tare values.

## Backend changes required

### 1) NFC text format
Store:
`Brand|Color|Type|Tare`

Example:
`Hatchbox|Red|PLA|178`

### 2) Read parsing
Extend your existing parse (Brand|Color|Type) to also read tare:

```python
parts = text.split('|')
brand = parts[0].strip() if len(parts) > 0 else ''
color = parts[1].strip() if len(parts) > 1 else ''
filament_type = parts[2].strip() if len(parts) > 2 else ''
tare = None
if len(parts) > 3:
    try:
        tare = float(parts[3].strip())
    except:
        tare = None

self.tare = tare
```

Include in `get_data()`:
```python
'tare': self.tare
```

### 3) Write payload includes tare
Change NFC write payload to:

```python
payload = f"{brand}|{color}|{filament_type}|{tare if tare is not None else ''}"
```

### 4) Update `/api/program_nfc/<slot_id>`
Accept `tare`:

```python
tare = data.get('tare', None)
if tare is not None and tare != '':
    try:
        tare = float(tare)
    except:
        return jsonify({'success': False, 'error': 'Invalid tare'}), 400
else:
    tare = None
```

Then call:
```python
ok = slot.write_nfc(brand, color, filament_type, tare)
```

### 5) Use tag tare in calculations
Where you subtract tare from measured weight, use `self.tare` (from tag) instead of per-slot stored tare.

---

## Result
- Move a roll to any port → tare follows the roll automatically.
- Works with dynamic/renumbered modular ports.