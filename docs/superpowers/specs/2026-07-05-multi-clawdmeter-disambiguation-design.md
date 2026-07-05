# Design: Disambiguate multiple Clawdmeters by name

**Date:** 2026-07-05
**Status:** Approved (pending spec review)

## Problem

An office runs two Clawdmeter boards, one per Mac. Every board advertises the
identical BLE name `Clawdmeter` and the identical service UUIDs (custom service
+ HID `0x1812`). Both Macs auto-connect (HID) to both boards, and each Mac's
daemon selects a board with *first-match-wins* logic
(`retrieve_connected_macos` in `daemon/claude_usage_daemon.py:244` takes the
first peripheral returned for the custom service). Because CoreBluetooth's
`retrieveConnectedPeripheralsWithServices_` ordering is not stable, each Mac
flip-flops between its own board and the colleague's board across cycles — the
display rotates to the wrong account and the connection is flaky.

### Why the existing single-owner lock is not enough

Commit `58c60cd` ("lock the board to a single owner machine") solves the
opposite direction: it stops a **second Mac** writing to **one board**. It does
*not* stop **one Mac** ambiguously targeting **two boards**. It also has a
first-pair-wins race (whichever Mac bonds to a board first becomes its permanent
owner), and even when it correctly rejects a wrong Mac's writes, the daemon
still wastes cycles trying the wrong board. This design removes both the race
(via deliberate naming) and the wasted cycles (via daemon-side name targeting).

## Scope

- **macOS only** for the daemon change (both office machines are Macs; the
  daemon path exercised is the CoreBluetooth `retrieveConnected` path).
- The firmware name change is platform-agnostic and benefits all hosts.
- Out of scope: Linux/Windows daemon changes; on-device rename UI; touching the
  owner-lock logic.

## Design

### A. Firmware — settable device name (all boards)

Location: `firmware/src/ble.cpp`, `firmware/src/ble.h`, `firmware/src/main.cpp`.

1. **NVS storage.** Reuse the existing `Preferences` store (namespace `"clawd"`,
   already holding `owner`). Add a `name` key holding the user-chosen suffix
   (empty when unset).

2. **Runtime advertised name.** Rename the compile-time
   `#define DEVICE_NAME "Clawdmeter"` to `DEVICE_NAME_BASE`. At `ble_init`, load
   the stored suffix and compute a runtime name buffer:
   - suffix empty  → `"Clawdmeter"` (unchanged default)
   - suffix set    → `"Clawdmeter-<suffix>"`

   Route the four current `DEVICE_NAME` uses through the runtime buffer:
   - `NimBLEDevice::init(name)` — sets GAP Device Name characteristic `0x2A00`,
     which the daemon reads.
   - `adv->setName(name)` (`ble.cpp:152`)
   - scan-response name (`ble.cpp:155`)
   - `ble_get_device_name()` return (`ble.cpp:331`)

3. **Serial commands** (in `main.cpp`, beside the existing `screenshot`
   dispatcher at `main.cpp:175`):
   - `name <value>` — validate, store to NVS, print confirmation, then
     `ESP.restart()` so the new name reapplies cleanly at init.
   - `name` (no argument) — print the current advertised name.
   - `name clear` — remove the NVS key, revert to `"Clawdmeter"`, restart.

   **Validation** for `<value>`: trim surrounding whitespace/CR/LF; allow only
   `[A-Za-z0-9-]`; **max 7 characters**. Rationale: the name also rides the
   primary advertising packet (`ble.cpp:144-152`), which is capped at 31 bytes.
   The current layout consumes 23 bytes with `"Clawdmeter"` (flags 3 +
   appearance 4 + HID UUID 4 + name AD 12), leaving 8 bytes of headroom; with
   `"Clawdmeter-"` (11 chars) a 7-char suffix yields an 18-char name (20-byte
   name AD) for exactly 31 bytes total. The **full, authoritative name** lives
   in GAP `0x2A00` (set via `NimBLEDevice::init`, no packet limit) — that is
   what the daemon matches on, so the 7-char cap is a cosmetic-packet constraint
   only. Reject invalid input with an error message and do **not** store or
   restart.

4. **Interaction with the owner-lock.** None. Bonds key off the BLE MAC, not the
   name, so renaming never disturbs pairing or ownership. No owner-lock code
   changes.

### B. Daemon — target board by name (macOS)

Location: `daemon/claude_usage_daemon.py`, `daemon/config.example`.

1. **Config option.** Add a `device` key to
   `~/.config/claude-usage-monitor/config` (re-read each poll, mirroring
   `chime`/`clock`). Add a `read_device_name()` helper. The value accepts either
   the bare suffix (`mor`) or the full name (`Clawdmeter-mor`); normalize to the
   full expected name (`Clawdmeter-mor`). **Unset → today's first-match
   behavior, fully backward-compatible.**

2. **Name confirmation on connect (approach ①).** In `connect_and_run`
   (`daemon/claude_usage_daemon.py:650`), immediately after the successful
   connect (after `log("Connected")`, `:683`) and before
   `session.setup_refresh_subscription()`:
   - If `device` is unset → proceed as today (no name check).
   - Else read GATT characteristic `0x2A00`
     (`00002a00-0000-1000-8000-00805f9b34fb`) and compare to the expected full
     name.
     - **Match** → remember the peripheral UUID as the preferred target
       (in-memory module global) and proceed normally.
     - **Mismatch** → log distinctly (e.g. `"Wrong board 'X' (want 'Y'),
       skipping"`), disconnect, and return `False`. On macOS the `main()` loop
       already maps a `False` return to `skip_addr = addr` (`:755`), so
       `discover_target` returns the *other* board next cycle. Convergence in
       ≤2 cycles.

   Reading `0x2A00` live sidesteps macOS's stale name cache in System Settings
   (the OS may still show the old name there; the GATT read returns the live
   value).

3. **Prefer the matched board (reduce churn).** `retrieve_connected_macos`
   (`:209`) should, when a preferred UUID is remembered from a prior match,
   return that peripheral first if it is among the connected custom-service
   peripherals. This avoids repeatedly grabbing the wrong board first when both
   are connected. The name read on connect remains the source of truth; the
   preference is only an ordering hint. In-memory for v1 (resets on daemon
   restart; re-converges within ≤2 cycles). File persistence is a possible
   future enhancement, not required.

### C. Error handling

- **Firmware:** invalid or too-long name → print an error, do not store, do not
  restart.
- **Daemon, configured board absent:** if `device` is set but no connected
  board's `0x2A00` matches, **wait** (log `waiting for 'Clawdmeter-mor'`) rather
  than falling back to first-match. Showing nothing beats showing a colleague's
  account; this also dovetails with the owner-lock, which would reject writes to
  the wrong board anyway.
- **Daemon, GAP read failure:** treat as a non-match → disconnect + skip + retry
  (same path as a mismatch).

## Testing

- **Firmware (manual, on hardware):**
  - `name mor` over serial → device re-advertises as `Clawdmeter-mor`
    (verify in macOS Bluetooth settings / nRF Connect).
  - Reboot → name persists (NVS).
  - `name` (no arg) → prints `Clawdmeter-mor`.
  - `name clear` → reverts to `Clawdmeter`.
  - Invalid input (too long / illegal chars) → rejected, name unchanged.
  - Owner-lock still functions (pair, verify single-owner behavior unchanged).
- **Daemon (unit, `daemon/tests/`):** pure-function tests for
  `read_device_name()` config parsing and full-name normalization (bare suffix
  vs. full name vs. unset).
- **End-to-end (later, via the `verify` skill):** two boards named differently,
  two Macs each with its own `device` configured → confirm no flip-flop across
  daemon restarts and that each Mac ignores the other's board.

## Docs / rollout

- Document `device` in `daemon/config.example` (with the bare-suffix vs.
  full-name note).
- Update `CLAUDE.md` (BLE + daemon sections) to describe per-board naming and
  the daemon `device` targeting option.

## Explicitly deferred (not in v1)

- Rendering the board name on the on-device Bluetooth screen. Low effort on S3,
  but C6 boards cannot screenshot (would need hardware eyeballing), so it is
  left out of v1. Revisit if desired.
- Linux/Windows daemon parity.
- File-persisting the daemon's matched-UUID preference across restarts.
