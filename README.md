# Instrument Suite: Fast Edition

[![Platform: Windows x64](https://img.shields.io/badge/Platform-Windows%20x64-blue.svg)](https://github.com/izoUwU/instrument-suite-fast/releases)
[![Language: Python 3](https://img.shields.io/badge/Language-Python%203-blue.svg)](https://www.python.org/)
[![Protocol: SCPI / GPIB](https://img.shields.io/badge/Protocol-SCPI%20%2F%20GPIB%20(PyVISA)-green.svg)](https://pyvisa.readthedocs.io/)
[![GUI: Tkinter / Canvas](https://img.shields.io/badge/GUI-Tkinter%20%7C%20Fast%20Canvas-orange.svg)](https://docs.python.org/3/library/tkinter.html)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Instrument Suite: Fast Edition** is a high-performance multi-threaded instrument control, real-time logging, and decimation plotting application developed for cleanroom and experimental physics laboratories.

Designed to eliminate measurement latency and user interface freezes during long-duration experiments, the suite uses a producer-consumer threaded architecture where each benchtop instrument is queried on an isolated worker thread while the main thread renders smooth, downsampled canvas strip-charts at 60 FPS.

---

## Download Standalone Windows Binary

Pre-compiled standalone Windows executable packages (requiring zero Python or driver installation) are available directly under [Releases](https://github.com/izoUwU/instrument-suite-fast/releases):

* **[Download Instrument Suite Fast v1.0.0 (Windows x64 .zip)](https://github.com/izoUwU/instrument-suite-fast/releases/download/v1.0.0/InstrumentSuiteFast-v1.0.0-windows-x64.zip)**

*To run:* Extract the `.zip` archive and run `InstrumentSuiteFast.exe`.

---

## Supported Laboratory Instruments

The software provides dedicated control and data-logging tabs for core electrical characterization instruments:

1. **Keithley Electrometer & Picoammeter** (e.g. Keithley 6517B, 6485)
   - Ultra-low current measurement (down to fA resolution).
   - High-voltage sourcing and stepped I-V sweep automation.
2. **Fluke 8846A 6.5-Digit Precision Multimeter**
   - High-precision DC voltage, 4-wire resistance, and frequency logging.
   - Configurable aperture times and sampling rates.
3. **Andeen-Hagerling AH2550A Ultra-Precision Capacitance Bridge**
   - 1 kHz ultra-low loss capacitance ($C$) and loss ($D$) measurements.
   - Sub-femtofarad resolution for nano-scale variable capacitors and membrane standoff characterization.
4. **Dual-Device Synchronous Acquisition Mode**
   - Simultaneous acquisition across two independent instruments (e.g. bias voltage monitoring alongside capacitance/current logging).

---

## System Architecture

```
+-----------------------------------------------------------+
|                    Tkinter User Interface                 |
|  [Tab: Keithley]  [Tab: Fluke]  [Tab: AH2550A]  [Dual Mode] |
|                                                           |
|    +--------------------------------------------------+   |
|    |      Fast Canvas Strip-Chart Plotting Engine     |   |
|    |    (Dynamic decimation to ~1500 line segments)   |   |
|    +--------------------------------------------------+   |
+-----------------------------^-----------------------------+
                              | Thread-safe Queue
+-----------------------------+-----------------------------+
|               Isolated Instrument Worker Threads          |
|                                                           |
|   Thread 1: Keithley        Thread 2: Fluke 8846A         |
|   - PyVISA READ? queries    - Status polling              |
|   - SCPI error queue check  - Zero-lag decoupled stream   |
+-----------------------------------------------------------+
                              |
                     GPIB / USB / RS-232
                              |
               [Physical Benchtop Hardware]
```

### Key Engineering Features

- **Non-Blocking Multithreaded Queue Engine**:
  GPIB query commands (such as `:READ?` or `FETC?`) often take 50–500 ms depending on instrument integration cycles. By offloading VISA communication to dedicated worker threads feeding thread-safe FIFO queues, the GUI event loop remains fully responsive with zero stutter.
- **Custom Decimation Plotting**:
  Standard Matplotlib or Tkinter canvas plotting degrades severely when millions of data points accumulate over hours of continuous logging. This engine dynamically downsamples incoming data streams to ~1,500 visual segments across the viewport width, maintaining instantaneous panning and 60 FPS refresh rates on standard lab workstations.
- **Pre-Run State Verification & Safety Interlocks**:
  Before logging commences, the software queries instrument function states and interrogates the SCPI error queue (`SYST:ERR?`), catching misconfigured ranges, compliance limits, or floating connections before corrupted data is saved.
- **Data Export & Diagnostics**:
  - Live elapsed run timers.
  - One-click CSV export with ISO-8601 timestamps and parameter headers.
  - Hardware simulation mode for offline UI testing and protocol development.

---

## Lab Deployment Notice

The full Python source code and laboratory-specific calibration profiles are maintained directly in the research group laboratory environment at the University of Arkansas. The pre-compiled executable bundle provided in this repository allows collaborators and laboratory users to deploy and run the suite across Windows-based instrument stations.

---

## License

This software is released under the [MIT License](LICENSE).

---

## Author & Contact

**Tamzeed Bani Amin**  
Department of Physics, University of Arkansas, Fayetteville, AR  
Email: [tbamin@uark.edu](mailto:tbamin@uark.edu)  
LinkedIn: [linkedin.com/in/tamzeed-bani-amin](https://linkedin.com/in/tamzeed-bani-amin)
