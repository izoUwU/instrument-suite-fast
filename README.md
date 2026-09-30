# Instrument Suite: Fast Edition

[![Platform: Windows x64](https://img.shields.io/badge/Platform-Windows%20x64-blue.svg)](https://github.com/izoUwU/instrument-suite-fast/releases)
[![Language: Python 3.11+](https://img.shields.io/badge/Language-Python%203.11+-blue.svg)](https://www.python.org/)
[![Protocol: SCPI / GPIB](https://img.shields.io/badge/Protocol-SCPI%20%2F%20GPIB%20(PyVISA)-green.svg)](https://pyvisa.readthedocs.io/)
[![GUI: Tkinter / Fast Canvas](https://img.shields.io/badge/GUI-Tkinter%20%7C%20FastCanvas-orange.svg)](https://docs.python.org/3/library/tkinter.html)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Instrument Suite: Fast Edition** is a high-performance multi-instrument control, real-time logging, and decimation plotting application developed for cleanroom and experimental physics laboratories.

Designed specifically to eliminate measurement latency and user interface freezes during continuous, multi-hour experimental runs, the suite replaces bloated plotting stacks with a custom, zero-overhead threaded architecture optimized for responsiveness on modest laboratory PCs.

---

## Download Standalone Windows Binary

Pre-compiled standalone Windows executable packages (requiring zero Python or driver installation) are available directly under [Releases](https://github.com/izoUwU/instrument-suite-fast/releases):

* **[Download Instrument Suite Fast v1.0.0 (Windows x64 .zip)](https://github.com/izoUwU/instrument-suite-fast/releases/download/v1.0.0/InstrumentSuiteFast-v1.0.0-windows-x64.zip)**

*To run:* Extract the `.zip` archive and run `InstrumentSuiteFast.exe`.

---

## Supported Laboratory Instruments

The software provides dedicated control and data-logging tabs for core electrical characterization instruments:

1. **Keithley Electrometer & Picoammeter** (Keithley 6517B, 6485)
   - Ultra-low current measurement (down to fA resolution).
   - High-voltage sourcing and stepped I-V sweep automation.
   - Dual-device mode supporting simultaneous voltage sourcing and multi-channel current logging.
2. **Fluke 8846A 6.5-Digit Precision Multimeter**
   - High-precision DC voltage, 4-wire resistance, and frequency logging.
   - Configurable aperture integration (NPLC options) and digital filtering.
3. **Andeen-Hagerling AH2550A Ultra-Precision Capacitance Bridge**
   - 1 kHz ultra-low loss capacitance ($C$) and loss ($D$/$G$) measurements.
   - Direct PyVISA command protocol (`"Q"`) with regex parsing (`C= ... PF L= ... NS`).
   - Sub-femtofarad resolution for nano-scale variable capacitors and membrane standoff characterization.
4. **Dual-Device Synchronous Acquisition Mode**
   - Simultaneous acquisition across two independent instruments (e.g. bias voltage monitoring alongside capacitance/current logging).

---

## System Architecture

```text
+-----------------------------------------------------------------------------------+
|                        Tkinter UI Layer & FastPlot Canvas                         |
|  [Tab: Keithley 6517B]  [Tab: Keithley 6485]  [Tab: Fluke 8846A]  [Tab: AH2550A]  |
|                                                                                   |
|   +---------------------------------------------------------------------------+   |
|   |         Pre-allocated tk.Canvas Strip-Chart Viewport (FastPlot)           |   |
|   |   - Dynamic Min/Max Bucket Decimation (caps traces to <= 1,500 segments)  |   |
|   |   - Pre-allocated graphic objects: updates canvas.coords() without redraw |   |
|   |   - Multi-pane synchronized time axes & live engineering tick formatters  |   |
|   +-------------------------------------^-------------------------------------+   |
|                                         | 30 Hz Viewport Redraw Timer (REDRAW_HZ) |
|   +-------------------------------------+-------------------------------------+   |
|   |            In-Memory Data Structures (collections.deque ring buffers)     |   |
|   +-------------------------------------^-------------------------------------+   |
|                                         | 60 Hz Queue Drain Timer (DRAIN_HZ)      |
+-----------------------------------------|-----------------------------------------+
                                          | Thread-Safe FIFO (queue.Queue)
+-----------------------------------------+-----------------------------------------+
|                  Isolated Worker Threads (threading.Thread)                       |
|                                                                                   |
|   Worker Thread 1 (Keithley)                Worker Thread 2 (Fluke / AH2550A)     |
|   - PyVISA query (":READ?", "Q")            - Non-blocking hardware I/O loop      |
|   - SCPI error validation (SYST:ERR?)       - Regex parsing ("C=...PF L=...NS")   |
+-----------------------------------------+-----------------------------------------+
                                          |
                      VISA Layer (pyvisa / NI-VISA / Keysight)
                                          |
                          GPIB / USB-TMC / RS-232 Interfaces
                                          |
                  [ Physical Benchtop Laboratory Hardware ]
```

### Key Engineering Features

1. **Dual-Rate Decoupled Event Loops**:
   - **VISA Polling Threads**: One dedicated background worker thread per active instrument handles slow hardware queries (such as `:READ?` or `:FETC?`, which take 50–500 ms depending on NPLC integration), pushing parsed measurement tuples into a thread-safe `queue.Queue`.
   - **Fast Queue Drain Loop (`DRAIN_HZ = 60`)**: The GUI event loop drains incoming samples at 60 Hz into memory ring buffers (`collections.deque`), ensuring zero hardware backpressure.
   - **Decoupled Viewport Redraw (`REDRAW_HZ = 30`)**: Strip-charts are redrawn on an independent 30 Hz timer, completely decoupling graphics rendering from hardware sampling rates.

2. **Zero-Allocation Canvas Engine (`FastPlot`)**:
   - Instead of using heavyweight plotting libraries (e.g. Matplotlib) which re-allocate thousands of artist objects every frame and trigger garbage collection pauses, `FastPlot` pre-allocates Tkinter canvas line items and directly updates their coordinates via `canvas.coords()`.
   - **Min/Max Bucket Decimation**: Automatically downsamples millions of accumulated data points into a fixed budget of $\le 1,500$ screen segments (`MAX_PLOT_SEGMENTS`), keeping CPU utilization below 2% even after days of continuous acquisition.

3. **Direct Lightweight Drivers (No PyMeasure / No Bloat)**:
   - Eliminates bloated third-party wrappers. The AH2550A capacitance bridge communicates via direct PyVISA ASCII strings with fast regex extraction (`_RE_CLV`), cutting query overhead in half.
   - Cold start time (interpreter launch to interactive UI) is well under one second.

4. **Pre-Run State Verification & Safety Interlocks**:
   - Before data logging starts, the setup sequence reads back instrument meter functions and interrogates the SCPI error queue (`SYST:ERR?`), catching compliance trips, open circuits, or invalid ranges before data is recorded.
   - Includes live elapsed timers, independent diagnostic log windows (`LiveLogWindow`), and automatic CSV export with ISO-8601 timestamps and parameter headers.

---

## Lab Deployment Notice

The full Python source code and laboratory-specific calibration profiles are maintained directly in the research group laboratory environment at the University of Arkansas. The pre-compiled executable bundle provided in this repository allows collaborators and laboratory users to deploy and run the suite across Windows-based instrument stations.

---

## Technical Stack

- **Language**: Python 3.11+
- **GUI**: Tkinter / `ttk` with dark laboratory theme (`#12141a` base palette)
- **Instrument Control**: PyVISA (`pyvisa`) over GPIB, USB-TMC, RS-232
- **Data Structures**: `collections.deque`, `queue.Queue`, `dataclasses`
- **Compiler**: PyInstaller (packaged as single-directory standalone Windows executable)

---

## License

This software is released under the [MIT License](LICENSE).

---

## Author & Contact

**Tamzeed Bani Amin**  
Department of Physics, University of Arkansas, Fayetteville, AR  
Email: [tbamin@uark.edu](mailto:tbamin@uark.edu)  
LinkedIn: [linkedin.com/in/tamzeed-bani-amin](https://linkedin.com/in/tamzeed-bani-amin)
