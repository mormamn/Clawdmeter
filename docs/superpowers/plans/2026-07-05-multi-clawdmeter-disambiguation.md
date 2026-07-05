# Multi-Clawdmeter Name Disambiguation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each Clawdmeter board be given a human name (`Clawdmeter-<name>`) via a serial command, and let each macOS daemon target its board by that name, so two boards in one office stop being confused for one another.

**Architecture:** Firmware stores an optional name suffix in NVS and advertises `Clawdmeter-<suffix>` (or `Clawdmeter` when unset), setting the GAP Device Name characteristic `0x2A00` from it. The macOS daemon reads a `device` config option, and after connecting to a candidate peripheral reads `0x2A00` to confirm it is the configured board — on mismatch it disconnects and reuses the existing `skip_addr` retry path to converge on the correct board next cycle.

**Tech Stack:** ESP32 Arduino / NimBLE-Arduino (`Preferences` NVS) for firmware; Python 3 + `bleak` (CoreBluetooth backend) for the daemon; `pytest` for daemon unit tests.

## Global Constraints

- Name suffix: only `[A-Za-z0-9-]`, **max 7 characters** (primary advertising packet is 31 bytes; 7 is the max that fits over `"Clawdmeter-"`). Copied verbatim from spec §A.3.
- Full authoritative name lives in GAP characteristic `0x2A00` (UUID `00002a00-0000-1000-8000-00805f9b34fb`); the daemon matches on that, not on advertising data.
- Default behavior must be unchanged: unset firmware name → advertises `Clawdmeter`; unset daemon `device` → today's first-match behavior.
- No `#ifdef BOARD_*` in shared code (project rule). All firmware changes here are in shared files (`ble.cpp`, `ble.h`, `main.cpp`) and are board-agnostic.
- NVS namespace is `"clawd"` (already used for the `owner` key); the name key is `"name"`.
- Daemon config file: `~/.config/claude-usage-monitor/config`, `key = value`, re-read each poll, `#` comments stripped (mirror `read_chime_setting`).
- Firmware has no host-side test harness; firmware tasks are verified manually over serial + a BLE scanner. Daemon logic is verified with `pytest`.
- Branch: `feature/clawdmeter-name-disambiguation` (already created).

---

### Task 1: Firmware — NVS-backed device name + serial commands

**Files:**
- Modify: `firmware/src/ble.cpp` (name storage/helpers + route the 4 `DEVICE_NAME` uses; `:7`, `:141-152`, `:252-253`, `:329-331`)
- Modify: `firmware/src/ble.h` (declare the three new functions)
- Modify: `firmware/src/main.cpp` (serial dispatcher `:170-182`)
- Test: manual (serial + BLE scanner) — no host harness for firmware

**Interfaces:**
- Produces (declared in `ble.h`, defined in `ble.cpp`):
  - `bool ble_set_name(const char* suffix);` — validate + persist suffix to NVS. Returns `true` if stored, `false` if invalid (caller does not restart on `false`).
  - `void ble_clear_name(void);` — remove the NVS `name` key.
  - `const char* ble_get_device_name(void);` — returns the current full runtime name (already exists at `ble.cpp:329-331`; behavior changes from returning the `#define` to returning the computed buffer).
- Consumes: existing static `Preferences prefs;` and namespace `"clawd"` in `ble.cpp:81`.

- [ ] **Step 1: Rename the compile-time constant to a base**

In `firmware/src/ble.cpp`, change line 7:

```cpp
#define DEVICE_NAME_BASE "Clawdmeter"
```

- [ ] **Step 2: Add the runtime name buffer + load/compute helper**

In `firmware/src/ble.cpp`, just below the owner-lock block (after `claim_owner`, near line 138), add:

```cpp
// --- Configurable device name ----------------------------------------------
// Optional user-set suffix persisted in NVS ("clawd"/"name"). Empty => the
// board advertises as "Clawdmeter"; set => "Clawdmeter-<suffix>". The full
// name is what NimBLEDevice::init() publishes as GAP char 0x2A00, which the
// host daemon reads to tell two boards apart. Suffix is [A-Za-z0-9-], <=7
// chars (keeps the 31-byte primary advertising packet from overflowing).
static char device_name[24] = DEVICE_NAME_BASE;  // "Clawdmeter" + "-" + <=7 + NUL

static bool name_char_ok(char c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
           (c >= '0' && c <= '9') || c == '-';
}

static void compute_device_name() {
    prefs.begin("clawd", true);
    String suffix = prefs.getString("name", "");
    prefs.end();
    if (suffix.length() == 0) {
        strncpy(device_name, DEVICE_NAME_BASE, sizeof(device_name) - 1);
    } else {
        snprintf(device_name, sizeof(device_name), "%s-%s",
                 DEVICE_NAME_BASE, suffix.c_str());
    }
    device_name[sizeof(device_name) - 1] = '\0';
}

bool ble_set_name(const char* suffix) {
    size_t n = strlen(suffix);
    if (n == 0 || n > 7) {
        Serial.println("BLE: name must be 1-7 chars of [A-Za-z0-9-]");
        return false;
    }
    for (size_t i = 0; i < n; i++) {
        if (!name_char_ok(suffix[i])) {
            Serial.println("BLE: name must be 1-7 chars of [A-Za-z0-9-]");
            return false;
        }
    }
    prefs.begin("clawd", false);
    prefs.putString("name", suffix);
    prefs.end();
    Serial.printf("BLE: name set to %s-%s\n", DEVICE_NAME_BASE, suffix);
    return true;
}

void ble_clear_name(void) {
    prefs.begin("clawd", false);
    prefs.remove("name");
    prefs.end();
    Serial.println("BLE: name cleared (reverts to " DEVICE_NAME_BASE ")");
}
```

- [ ] **Step 3: Compute the name at init and route all four uses through it**

In `firmware/src/ble.cpp`:

At the very top of `ble_init(void)` (before `NimBLEDevice::init(...)` at `:253`), add:

```cpp
    compute_device_name();
```

Change `:253` from `NimBLEDevice::init(DEVICE_NAME);` to:

```cpp
    NimBLEDevice::init(device_name);
```

Change the advertising name at `:152` from `adv->setName(DEVICE_NAME);` to:

```cpp
    adv->setName(device_name);
```

Change `ble_get_device_name` at `:329-331` to return the buffer:

```cpp
const char* ble_get_device_name(void) {
    return device_name;
}
```

(If `ble_get_device_name` currently returns `DEVICE_NAME`, this is the only body change. Leave `ble_get_mac_address` untouched.)

- [ ] **Step 4: Declare the new functions in the header**

In `firmware/src/ble.h`, alongside the existing `ble_get_device_name` declaration, add:

```cpp
bool ble_set_name(const char* suffix);
void ble_clear_name(void);
```

- [ ] **Step 5: Wire the serial commands**

In `firmware/src/main.cpp`, extend the dispatcher at `:175-176`. Replace:

```cpp
            if (strcmp(cmd_buf, "screenshot") == 0) send_screenshot();
            else if (strcmp(cmd_buf, "buzz") == 0)  sound_hal_play_reset();
```

with:

```cpp
            if (strcmp(cmd_buf, "screenshot") == 0) send_screenshot();
            else if (strcmp(cmd_buf, "buzz") == 0)  sound_hal_play_reset();
            else if (strcmp(cmd_buf, "name") == 0) {
                Serial.printf("name: %s\n", ble_get_device_name());
            } else if (strcmp(cmd_buf, "name clear") == 0) {
                ble_clear_name();
                delay(50);
                ESP.restart();
            } else if (strncmp(cmd_buf, "name ", 5) == 0) {
                if (ble_set_name(cmd_buf + 5)) {
                    delay(50);
                    ESP.restart();
                }
            }
```

Ensure `main.cpp` includes `ble.h` (it already calls `ble_*` functions, so the include exists; if not, add `#include "ble.h"`).

- [ ] **Step 6: Build both a representative S3 and C6 env**

Run:
```bash
pio run -d firmware -e waveshare_amoled_216
pio run -d firmware -e waveshare_amoled_18_c6
```
Expected: both compile with no errors (`SUCCESS`). This proves the shared-code change builds on both SoC families.

- [ ] **Step 7: Manual hardware verification**

Flash one board, e.g.:
```bash
pio run -d firmware -e waveshare_amoled_18 -t upload --upload-port /dev/cu.usbmodem101
```
Open the serial monitor (`pio device monitor -e waveshare_amoled_18`), then:
1. Type `name` → prints `name: Clawdmeter`.
2. Type `name mor` → prints `BLE: name set to Clawdmeter-mor`, board reboots.
3. After reboot, confirm in macOS Bluetooth settings / nRF Connect it advertises `Clawdmeter-mor`; type `name` → prints `name: Clawdmeter-mor` (NVS persisted).
4. Type `name toolongxx` (8+ chars) → prints the `1-7 chars` error, no reboot, name unchanged.
5. Type `name clear` → reboots, `name` now prints `name: Clawdmeter`.
6. Pair a Mac and confirm the owner-lock still behaves (data writes accepted from the paired Mac; display updates).

- [ ] **Step 8: Commit**

```bash
git add firmware/src/ble.cpp firmware/src/ble.h firmware/src/main.cpp
git commit -m "firmware: settable device name via serial (Clawdmeter-<name>)"
```

---

### Task 2: Daemon — `device` config option + name normalization

**Files:**
- Modify: `daemon/claude_usage_daemon.py` (add readers near the other config readers, `:287-328`)
- Modify: `daemon/config.example` (document the option)
- Test: `daemon/tests/test_macos_multidir.py` (add cases; follows existing `mod.CONFIG_FILE` monkeypatch pattern)

**Interfaces:**
- Produces:
  - `normalize_device_name(raw: str) -> str | None` — trims; returns `None` for empty; if the value already starts with `"Clawdmeter"` returns it verbatim, else returns `f"Clawdmeter-{raw}"`.
  - `read_target_device() -> str | None` — reads the `device` key from `CONFIG_FILE`; returns the normalized full name, or `None` when unset/blank/unreadable.
- Consumes: module globals `CONFIG_FILE` (`:41`), `log` (`:57`).

- [ ] **Step 1: Write the failing tests**

In `daemon/tests/test_macos_multidir.py`, add at the end (the module already imports `daemon.claude_usage_daemon as mod`):

```python
# ---------------------------------------------------------------------------
# read_target_device / normalize_device_name
# ---------------------------------------------------------------------------

def test_normalize_device_name_bare_suffix():
    assert mod.normalize_device_name("mor") == "Clawdmeter-mor"

def test_normalize_device_name_full_name_passthrough():
    assert mod.normalize_device_name("Clawdmeter-mor") == "Clawdmeter-mor"

def test_normalize_device_name_bare_base_passthrough():
    assert mod.normalize_device_name("Clawdmeter") == "Clawdmeter"

def test_normalize_device_name_trims_and_blank_is_none():
    assert mod.normalize_device_name("  bob ") == "Clawdmeter-bob"
    assert mod.normalize_device_name("   ") is None

def test_target_device_unset_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "CONFIG_FILE", tmp_path / "config")  # absent
    assert mod.read_target_device() is None

def test_target_device_key_absent_returns_none(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("clock = auto\nchime = on\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    assert mod.read_target_device() is None

def test_target_device_bare_suffix(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("device = mor   # my board\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    assert mod.read_target_device() == "Clawdmeter-mor"

def test_target_device_full_name(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("device = Clawdmeter-alice\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    assert mod.read_target_device() == "Clawdmeter-alice"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest daemon/tests/test_macos_multidir.py -k "device or normalize" -q`
Expected: FAIL with `AttributeError: module ... has no attribute 'normalize_device_name'`.

- [ ] **Step 3: Implement the two functions**

In `daemon/claude_usage_daemon.py`, after `read_clock_setting` (`:328`), add:

```python
def normalize_device_name(raw: str) -> str | None:
    """Normalize a configured board name to its full advertised form.

    Accepts a bare suffix ("mor") or a full name ("Clawdmeter-mor"); returns
    the full name. Blank -> None (unset).
    """
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("Clawdmeter"):
        return raw
    return f"Clawdmeter-{raw}"


def read_target_device() -> str | None:
    """Read the `device` option (the board this Mac should bind to).

    Returns the full expected name (e.g. "Clawdmeter-mor"), or None when unset
    so the daemon keeps its original first-match behavior.
    """
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "device":
                    return normalize_device_name(val)
    except OSError:
        pass
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest daemon/tests/test_macos_multidir.py -k "device or normalize" -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Document the option in config.example**

In `daemon/config.example`, after the `clock = off` block, append:

```text

# Bind this machine's daemon to ONE specific board by name, so two Clawdmeters
# in the same room don't get confused for each other. Name a board over its USB
# serial console with `name <label>` (it will advertise as Clawdmeter-<label>).
#   - Value may be the bare label (mor) or the full name (Clawdmeter-mor).
#   - Set (default) = the daemon only ever talks to the matching board and
#     waits if it isn't present, rather than grabbing whichever it sees first.
#   - Unset (default) = original behavior: bind to the first Clawdmeter found.
# device = mor
```

- [ ] **Step 6: Commit**

```bash
git add daemon/claude_usage_daemon.py daemon/config.example daemon/tests/test_macos_multidir.py
git commit -m "daemon: add 'device' config option to target a named board"
```

---

### Task 3: Daemon — confirm the board by GATT name on connect

**Files:**
- Modify: `daemon/claude_usage_daemon.py`
  - `retrieve_connected_macos` (`:209-260`) — prefer a previously-matched peripheral UUID
  - `connect_and_run` (`:650-713`) — add `expected_name` param + `0x2A00` check
  - `main` (`:735-765`) — read the target once per attempt and pass it through
  - module globals near `:192` — add the preferred-UUID hint
- Test: `daemon/tests/test_macos_multidir.py` (async test of the mismatch path with a mocked client); manual E2E for the CoreBluetooth-bound parts

**Interfaces:**
- Consumes: `read_target_device()` (Task 2), `BleakClient`, `log`, `CONNECT_TIMEOUT`, existing `skip_addr` plumbing in `main`.
- Produces: `connect_and_run(target, stop_event, expected_name=None)` — new optional 3rd parameter. Returns `False` (existing "connection not used" semantics) on a name mismatch so `main`'s macOS branch sets `skip_addr = addr`.
- Adds constant `DEVICE_NAME_CHAR_UUID = "00002a00-0000-1000-8000-00805f9b34fb"` and module global `_preferred_uuid: str | None = None`.

- [ ] **Step 1: Write the failing test (mismatch path returns False and disconnects)**

In `daemon/tests/test_macos_multidir.py`, add:

```python
from unittest.mock import AsyncMock, MagicMock, patch  # already imported; keep one copy

def test_connect_wrong_board_name_returns_false(monkeypatch):
    """A connected peripheral whose 0x2A00 != expected is rejected (False)."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_connected = True
    # 0x2A00 reports a different board than we want.
    client.read_gatt_char = AsyncMock(return_value=b"Clawdmeter-other")

    monkeypatch.setattr(mod, "BleakClient", lambda *a, **k: client)
    stop = asyncio.Event()

    ok = _run(mod.connect_and_run("UUID-1", stop, expected_name="Clawdmeter-mor"))

    assert ok is False
    client.read_gatt_char.assert_awaited_once_with(mod.DEVICE_NAME_CHAR_UUID)
    client.disconnect.assert_awaited()  # we hung up on the wrong board
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest daemon/tests/test_macos_multidir.py::test_connect_wrong_board_name_returns_false -q`
Expected: FAIL — `connect_and_run` currently takes no `expected_name` (TypeError) or lacks the `read_gatt_char` check.

- [ ] **Step 3: Add the constant and preferred-UUID global**

In `daemon/claude_usage_daemon.py`, near the other UUID constants (`:28-30`), add:

```python
DEVICE_NAME_CHAR_UUID = "00002a00-0000-1000-8000-00805f9b34fb"  # GAP Device Name
```

Near the `_cb_manager = None` global (`:192`), add:

```python
_preferred_uuid = None  # macOS: peripheral UUID last confirmed as the target board
```

- [ ] **Step 4: Add the name check to `connect_and_run`**

In `daemon/claude_usage_daemon.py`, change the signature (`:650`) to:

```python
async def connect_and_run(target, stop_event: asyncio.Event, expected_name=None) -> bool:
```

Immediately after `log("Connected")` (`:683`) and before `session = Session(client)`, insert:

```python
        # Confirm this is the board this machine is bound to. On macOS the
        # peripheral name from retrieveConnected is often None, so read the GAP
        # Device Name characteristic (0x2A00) live. On a mismatch, hang up and
        # return False; main()'s macOS branch turns that into skip_addr so the
        # next cycle picks the OTHER connected board. Match => remember this
        # peripheral so we prefer it next time and stop churning.
        if expected_name:
            try:
                raw = await client.read_gatt_char(DEVICE_NAME_CHAR_UUID)
                actual = raw.decode("utf-8", "replace").rstrip("\x00")
            except (BleakError, asyncio.TimeoutError) as e:
                log(f"Could not read device name: {e}; skipping this peripheral")
                actual = None
            if actual != expected_name:
                log(f"Wrong board {actual!r} (want {expected_name!r}), skipping")
                try:
                    await client.disconnect()
                except BleakError:
                    pass
                return False
            global _preferred_uuid
            _preferred_uuid = target if isinstance(target, str) else target.address
            log(f"Confirmed target board {expected_name!r}")
```

- [ ] **Step 5: Prefer the matched peripheral in `retrieve_connected_macos`**

In `daemon/claude_usage_daemon.py`, inside `retrieve_connected_macos`, replace the custom-service loop (`:248-250`):

```python
    for p in custom or []:
        if _ok(p):
            return _wrap(p)
```

with a version that returns the previously-confirmed peripheral first:

```python
    candidates = [p for p in (custom or []) if _ok(p)]
    if _preferred_uuid:
        for p in candidates:
            if p.identifier().UUIDString() == _preferred_uuid:
                return _wrap(p)
    if candidates:
        return _wrap(candidates[0])
```

- [ ] **Step 6: Pass the configured target through `main`**

In `daemon/claude_usage_daemon.py`, in the `main` loop, after `target = await discover_target(...)` and the `if not target:` block (i.e. just before `ok = await connect_and_run(target, stop_event)` at `:750`), read the target name and pass it:

```python
        expected_name = read_target_device()
        ok = await connect_and_run(target, stop_event, expected_name=expected_name)
```

(Reading it here — inside the loop — keeps the config live-editable each cycle, matching how `chime`/`clock` are read.)

- [ ] **Step 7: Run the full daemon test file**

Run: `python -m pytest daemon/tests/test_macos_multidir.py -q`
Expected: PASS (all prior tests + the new mismatch test).

- [ ] **Step 8: Manual end-to-end verification (two boards, one Mac)**

With two boards named `Clawdmeter-mor` and `Clawdmeter-alice`, both paired to this Mac:
1. Set `device = mor` in `~/.config/claude-usage-monitor/config`.
2. Start the daemon; watch the log. Expected: it may `Wrong board 'Clawdmeter-alice', skipping` once, then `Confirmed target board 'Clawdmeter-mor'` and begins polling — and stays on `mor` across restarts (no flip-flop).
3. Power `mor` off: expected the daemon logs the wrong-board skip for `alice` and then waits (does not bind to `alice`).
4. Remove `device` from config: expected original first-match behavior returns.

- [ ] **Step 9: Commit**

```bash
git add daemon/claude_usage_daemon.py daemon/tests/test_macos_multidir.py
git commit -m "daemon: confirm target board via GATT 0x2A00 on connect"
```

---

### Task 4: Update project docs (CLAUDE.md)

**Files:**
- Modify: `CLAUDE.md` (the "Daemon / host side" discovery section, and a one-line note in the BLE architecture bullet)

**Interfaces:** none (documentation only).

- [ ] **Step 1: Update the daemon discovery notes**

In `CLAUDE.md`, under "Daemon / host side" → "Discovery & resilience", add a bullet:

```markdown
- **Per-board naming (multi-device offices).** A board can be named over USB
  serial (`name <label>` → advertises `Clawdmeter-<label>`, `name` to print,
  `name clear` to reset; label is `[A-Za-z0-9-]`, ≤7 chars, persisted in NVS,
  full name published as GAP `0x2A00`). Each Mac's daemon binds to one board via
  the `device = <label>` config key: on connect it reads `0x2A00` and, on a
  mismatch, disconnects and reuses the `skip_addr` retry path to converge on the
  right board (and waits, rather than grabbing the wrong one, if its board is
  absent). Unset `device` = original first-match behavior. macOS-only today.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document per-board naming + daemon device targeting"
```

---

## Self-Review

**1. Spec coverage:**
- §A firmware settable name → Task 1 (NVS key, runtime name, 4 uses routed, serial `name`/`name clear`/`name <value>`, validation). ✓
- §B daemon `device` + normalization → Task 2; GATT `0x2A00` confirm + preferred-UUID + skip reuse + live per-cycle read → Task 3. ✓
- §C error handling: firmware invalid-name reject (Task 1 Step 2, `ble_set_name` returns false, no restart); daemon absent-board waits (Task 3 — a lone wrong board is skipped and never bound; `discover_target` already logs/waits when none match); GAP read failure → skip (Task 3 Step 4 `except` sets `actual=None` → mismatch path). ✓
- §D testing: firmware manual (Task 1 Step 7), daemon unit (Task 2 Step 1, Task 3 Step 1), E2E (Task 3 Step 8). ✓
- §E docs: config.example (Task 2 Step 5), CLAUDE.md (Task 4). ✓
- Deferred items (on-screen render, Linux/Windows, file-persisted UUID) → not tasked, matches spec. ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to". Every code step shows full code. ✓

**3. Type consistency:** `ble_set_name`/`ble_clear_name`/`ble_get_device_name` consistent between `ble.h` (Task 1 Step 4) and `ble.cpp` (Steps 2-3) and `main.cpp` (Step 5). `normalize_device_name`/`read_target_device` names/return types consistent between Task 2 (definition) and Task 3 (Step 6 usage). `DEVICE_NAME_CHAR_UUID` and `_preferred_uuid` defined (Task 3 Step 3) before use (Steps 4-5). `connect_and_run(..., expected_name=None)` signature (Step 4) matches the call site (Step 6) and the test (Step 1). ✓
