# Pico Shutter Trigger Design

Hardware trigger for Fujifilm X-T4 (and any camera with a 2.5mm remote
release) using a Raspberry Pi Pico and an optocoupler.

## Hardware

### Bill of Materials

| Part | Spec | Notes |
|------|------|-------|
| Raspberry Pi Pico | RP2040 | Any variant (Pico, Pico W, Pico 2) |
| Optocoupler | PC817 (or equivalent 4-pin DIP) | Galvanic isolation from camera |
| Resistor | 330R 1/4W | Current-limit for PC817 LED side |
| 2.5mm TS plug | Mono, solder-type, right-angle preferred | Tip = shutter, Sleeve = ground |
| Wire | Thin hookup wire or cut cable | ~30cm to camera |

No focus control needed (manual focus locked on the sun), so a mono TS
plug is sufficient. Only tip and sleeve are used.

### Circuit

```
         Pico                    PC817                 2.5mm plug
    +-----------+          +-------------+
    |           |          |  1    4     |         Tip ──────┐
    |  GPIO 15 ├───[330R]──┤ LED  PhTr  ├─────────────────── │
    |           |          |             |                    │
    |      GND ├──────────┤  2    3     ├──────── Sleeve ───┘
    |           |          +-------------+
    +-----------+

    GPIO 15 HIGH → PC817 LED on → phototransistor saturates
                 → tip shorted to sleeve → shutter fires
```

Pin 1 (anode) ← 330R ← GPIO 15
Pin 2 (cathode) → Pico GND
Pin 3 (emitter) → 2.5mm sleeve (camera ground)
Pin 4 (collector) → 2.5mm tip (shutter fire)

The PC817 provides complete electrical isolation between the Pico and
the camera. A GPIO mishap cannot damage the camera.

### Pulse Timing

The camera needs the tip held low for long enough to register a shutter
press. Empirically ~50ms is reliable for Fuji. The PIO program uses a
configurable pulse width.

## Pico Firmware Architecture

### Why Not Pure MicroPython?

MicroPython on the RP2040 runs on Core 0. Its main event loop has
10-50us jitter, which is fine for scheduling shots within a totality
window. But MicroPython's GC pauses can occasionally spike to ~1ms,
and the GIL prevents true parallel execution with `_thread`.

The RP2040 offers three better mechanisms for the timing-critical part:

1. **PIO (Programmable I/O)** - cycle-accurate GPIO pulse, zero jitter
2. **Core 1** - dedicated realtime loop, independent of MicroPython
3. **Hardware timers** - 1us resolution, interrupt-driven

### Recommended: MicroPython + PIO Hybrid

Core 0 (MicroPython) handles serial USB communication and shot
scheduling. A PIO state machine handles the actual GPIO pulse with
hardware-level precision.

```
+----------------------------------------------------------------+
|  Raspberry Pi Pico (RP2040)                                    |
|                                                                |
|  Core 0 (MicroPython)           PIO State Machine 0            |
|  +-------------------------+    +-------------------------+    |
|  | USB serial ←→ workbench |    |  pull block   (wait)    |    |
|  | Parse commands           |--->|  set pins, 1  (fire)    |    |
|  | Schedule shots           |    |  <delay N cycles>       |    |
|  | sm.put(pulse_duration)   |    |  set pins, 0  (release) |    |
|  +-------------------------+    +-----------|-------------+    |
|                                             |                  |
|                                        GPIO 15 ───> PC817     |
+----------------------------------------------------------------+
```

### PIO Shutter Pulse Program (MicroPython)

```python
import rp2
from machine import Pin

@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def shutter_pulse():
    """Wait for a value in the TX FIFO, then pulse GPIO high for that
    many cycles. At freq=1_000_000 each cycle = 1us, so putting 50000
    gives a 50ms pulse."""
    wrap_target()
    pull(block)          # wait for Core 0 to sm.put(duration_us)
    mov(x, osr)          # load duration into X
    set(pins, 1)         # GPIO HIGH → shutter fires
    label("hold")
    jmp(x_dec, "hold")   # count down (1 cycle per iteration = 1us)
    set(pins, 0)         # GPIO LOW → release
    wrap()               # back to waiting

SHUTTER_PIN = 15

sm = rp2.StateMachine(
    0,
    shutter_pulse,
    freq=1_000_000,      # 1MHz → 1us per cycle
    set_base=Pin(SHUTTER_PIN),
)
sm.active(1)

def fire(duration_ms=50):
    """Fire the shutter with a pulse of duration_ms milliseconds."""
    sm.put(duration_ms * 1000)  # convert ms → us (cycles at 1MHz)
```

### Burst via PIO

For rapid burst shooting, pre-feed multiple values into the PIO TX FIFO
(4 words deep) or loop on Core 0:

```python
import time

def burst(count, interval_ms=200, pulse_ms=50):
    """Fire count shots with interval_ms between each."""
    for i in range(count):
        fire(pulse_ms)
        if i < count - 1:
            time.sleep_ms(interval_ms)
```

The PIO handles each individual pulse with zero jitter. The interval
between shots has MicroPython-level jitter (~50us) which is irrelevant
at these timescales.

### Alternative: Dedicated Core 1 (C SDK)

If you need tighter inter-shot timing (sub-millisecond intervals for
mechanical shutter burst), drop to C SDK on Core 1:

```c
// core1_trigger.c - runs on Core 1, zero MicroPython involvement

#include "pico/multicore.h"
#include "hardware/gpio.h"
#include "hardware/timer.h"

#define SHUTTER_PIN 15

// Command encoding: upper 16 bits = count, lower 16 bits = pulse_ms
// Special: 0xFFFFFFFF = single fire with default 50ms pulse

void core1_entry() {
    gpio_init(SHUTTER_PIN);
    gpio_set_dir(SHUTTER_PIN, GPIO_OUT);
    gpio_put(SHUTTER_PIN, 0);

    while (true) {
        // Block until Core 0 sends a command via FIFO
        uint32_t cmd = multicore_fifo_pop_blocking();

        uint16_t count    = (cmd >> 16) & 0xFFFF;
        uint16_t pulse_ms = cmd & 0xFFFF;
        if (count == 0) count = 1;
        if (pulse_ms == 0) pulse_ms = 50;

        for (uint16_t i = 0; i < count; i++) {
            gpio_put(SHUTTER_PIN, 1);
            busy_wait_us(pulse_ms * 1000);
            gpio_put(SHUTTER_PIN, 0);

            if (i < count - 1) {
                // Inter-shot gap: could receive from second FIFO word
                busy_wait_us(200 * 1000);  // 200ms default
            }
        }

        // Signal completion back to Core 0
        multicore_fifo_push_blocking(count);
    }
}
```

From MicroPython on Core 0, you can still use `_thread` to poke the
FIFO, but the cleaner path is a small C module compiled into the
MicroPython firmware that exposes `trigger.fire(count, pulse_ms)`.

### Which Approach to Use?

| Approach | Jitter | Complexity | When to use |
|----------|--------|-----------|-------------|
| PIO (recommended) | ~0 (cycle-accurate pulse) | Low (pure MicroPython) | Default choice. Pulse is perfect, scheduling is good enough |
| Core 1 C + MicroPython | ~0.25us | Medium (custom firmware build) | Only if you need sub-ms inter-shot timing |
| Pure MicroPython GPIO | ~1ms (GC spikes) | Lowest | Prototyping only, don't use for eclipse day |

**Recommendation: PIO approach.** The pulse accuracy is hardware-perfect,
and MicroPython's scheduling jitter (tens of microseconds) is irrelevant
when your shot intervals are 200ms+.

## Serial Protocol

A simple line-based ASCII protocol over USB CDC serial. This is the
"common language" that lets the workbench talk to the Pico trigger the
same way it talks to USB SDK cameras.

### Commands (Host → Pico)

```
PING\n                    → alive check
FIRE\n                    → single shot, default 50ms pulse
FIRE:100\n                → single shot, 100ms pulse
BURST:5:200\n             → 5 shots, 200ms interval
BURST:10:100:30\n         → 10 shots, 100ms interval, 30ms pulse
ID\n                      → return device identity
```

### Responses (Pico → Host)

```
OK\n                      → command accepted
OK:FIRED:1\n              → single shot complete
OK:FIRED:5\n              → burst of 5 complete
PONG\n                    → response to PING
ID:PICO_TRIGGER:v1\n      → identity response
ERR:UNKNOWN_CMD\n         → unrecognized command
ERR:BUSY\n                → previous burst still running
```

### Pico Main Loop (MicroPython)

```python
import sys
import select

# sm and fire() defined above from PIO section

poll = select.poll()
poll.register(sys.stdin, select.POLLIN)

busy = False

while True:
    events = poll.poll(100)  # 100ms timeout
    if not events:
        continue

    line = sys.stdin.readline().strip()
    if not line:
        continue

    if line == "PING":
        print("PONG")
    elif line == "ID":
        print("ID:PICO_TRIGGER:v1")
    elif line.startswith("FIRE"):
        parts = line.split(":")
        pulse_ms = int(parts[1]) if len(parts) > 1 else 50
        fire(pulse_ms)
        print(f"OK:FIRED:1")
    elif line.startswith("BURST"):
        parts = line.split(":")
        count = int(parts[1])
        interval_ms = int(parts[2]) if len(parts) > 2 else 200
        pulse_ms = int(parts[3]) if len(parts) > 3 else 50
        burst(count, interval_ms, pulse_ms)
        print(f"OK:FIRED:{count}")
    else:
        print("ERR:UNKNOWN_CMD")
```

## Workbench Integration

### PicoCamera Adapter

A new `BaseCamera` subclass that speaks the serial protocol. The
workbench doesn't know or care that it's a dumb GPIO trigger vs a
full USB SDK — it calls `capture()` and `burst()` the same way.

```python
# src/solareclipseworkbench/camera/pico.py

import serial
from .types import BaseCamera


class PicoCamera(BaseCamera):
    """Camera triggered via Pico serial GPIO trigger.

    Settings (shutter speed, aperture, ISO) must be set on the camera
    body manually. This adapter only controls the shutter fire signal.
    """

    vendor = "Pico"

    def __init__(self, port: str, name: str = "Pico Trigger",
                 baudrate: int = 115200):
        super().__init__(name)
        self._port = port
        self._baudrate = baudrate
        self._serial = None

    def connect(self) -> None:
        self._serial = serial.Serial(self._port, self._baudrate, timeout=2)
        self._serial.write(b"PING\n")
        resp = self._serial.readline().decode().strip()
        if resp != "PONG":
            raise ConnectionError(f"Pico not responding (got: {resp!r})")
        self._connected = True

    def disconnect(self) -> None:
        if self._serial:
            self._serial.close()
        self._connected = False

    def configure(self, **kwargs) -> None:
        # No-op: settings are on the camera body.
        pass

    def capture(self):
        self._send("FIRE")

    def burst(self, count: int, interval_ms: int = 200):
        self._send(f"BURST:{count}:{interval_ms}")

    def _send(self, cmd: str) -> str:
        self._serial.write(f"{cmd}\n".encode())
        resp = self._serial.readline().decode().strip()
        if resp.startswith("ERR"):
            raise RuntimeError(f"Pico trigger error: {resp}")
        return resp
```

### Unified Dispatch

The existing `capture.py` already handles non-gphoto cameras by
checking `isinstance(camera, BaseCamera)` and calling `camera.capture()`.
A `PicoCamera` works with zero changes to the scheduling engine:

```python
# In capture.py _adapt_camera_settings():
#   isinstance(camera, BaseCamera) and not hasattr(camera, '_camera')
#   → returns (None, None)
#   → caller does camera.capture()
#
# PicoCamera.capture() sends "FIRE\n" over serial. Done.
```

For burst, `take_burst()` checks `camera.vendor`. Add a branch:

```python
elif getattr(camera, 'vendor', None) == 'Pico':
    camera.burst(max(1, int(round(duration))), interval_ms=200)
    return
```

### Discovery

```python
# In discovery.py
import serial.tools.list_ports

def detect_pico_triggers() -> dict[str, PicoCamera]:
    """Scan serial ports for Pico triggers."""
    triggers = {}
    for port in serial.tools.list_ports.comports():
        if "Pico" in (port.product or "") or "2e8a" in (port.vid or ""):
            try:
                cam = PicoCamera(port.device)
                cam.connect()
                triggers[f"Pico@{port.device}"] = cam
            except Exception:
                pass
    return triggers
```

## How This Fits the "Universal Language"

The three camera control paths all converge at `BaseCamera`:

```
                    BaseCamera
                    /    |    \
          GPhoto2  /     |     \  FujiCamera    PicoCamera
         Adapter  /      |      \
        (Canon,  /       |       \
        Nikon)  /        |        \
               /         |         \
        libgphoto2   Fuji X SDK   Serial "FIRE\n"
        over USB     over USB     over USB CDC
           |             |             |
        Canon/Nikon   Fuji X-T4    Any camera with
        PTP protocol  PTP variant  2.5mm jack
```

The workbench scheduling engine calls `camera.capture()` regardless.
Each adapter translates that into the right wire protocol. The Pico
trigger is the simplest — it's literally a remote shutter button that
the computer can press.

The serial protocol is intentionally minimal so other microcontrollers
(ESP32, Arduino, etc.) can implement the same `FIRE`/`BURST`/`PING`
command set and be plug-compatible as a `PicoCamera`.
