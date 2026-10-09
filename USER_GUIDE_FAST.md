# Thibado Labs Instrument Suite -- Fast Edition User Guide

Welcome to the **Thibado Labs Instrument Suite (Fast Edition)**. This guide explains how to operate the application, configure measurement settings, and select the optimal speed/precision trade-offs for each supported instrument.

---

## 1. Fast Edition Architecture: Why It Is Fast

Unlike standard instrumentation scripts that freeze or lag during long acquisitions, the **Fast Edition** is engineered for maximum responsiveness and high-frequency data streams:

* **Multithreaded VISA Communication**: VISA operations (setup and reads) run on dedicated background worker threads (one per active device). The main GUI thread never blocks waiting for a GPIB query to return.
* **Decoupled Update Rates**: 
  * The GUI drains incoming data points from the thread-safe queue at **60 Hz** (`DRAIN_HZ`).
  * The visual display redraws at a steady **30 Hz** (`REDRAW_HZ`) to conserve CPU cycles while keeping animations fluid.
* **Min/Max-Bucket Decimation**: To prevent Tkinter from lagging under millions of data points, a custom plotting canvas buckets the dataset down to a maximum of **1500 segments** (`MAX_PLOT_SEGMENTS`) for rendering. This ensures that a 2-second run and a 24-hour run display instantly and responsively, while still capturing all critical noise spikes and transients.
* **Minimal Dependencies**: Matplotlib has been replaced with a native `tk.Canvas` plot, and Andeen-Hagerling communication runs on raw PyVISA rather than the bulkier `pymeasure` library. Cold startup time is under a second.

---

## 2. Common Interface Controls

The application layout is standardized across all tabs:

* **Measurement Selector**: Dropdown to select the active measurement mode. Changing this resets the plot and data buffer.
* **Interval (ms)**: The polling interval in milliseconds.
  * *Fast Readings (0 ms)*: Commands the background thread to query the instrument continuously as fast as the physical hardware allows.
  * *Timed Readings (e.g., 100 ms)*: Standard interval for regular logging.
* **Device Addresses**: Text inputs (with dropdown list populated by a VISA resource scan) for Device A and Device B (if in Dual Mode).
* **Dual Device Toggle**: Enables simultaneous acquisition from two identical instruments using the same configuration.
* **View Controls**:
  * **Rolling (Checked)**: Shows a moving window of the last $N$ seconds (defined in the entry box). Perfect for inspecting transient behaviors or stability.
  * **Rolling (Unchecked)**: Displays the full history since clicking **Start**.
* **Live Log Window**: A separate, memory-limited pop-out window showing high-precision text output. Excellent for verifying numeric stability without cluttering the screen.
* **Save CSV**: Exports the full high-precision float dataset (using scientific notation `%.9e`) to `~/InstrumentSuiteData/` by default.

---

## 3. Instrument-Specific Settings & Optimization

Selecting the correct integration, bandwidth, and averaging parameters is critical to getting valid data. Below are detailed recommendations for each instrument class.

### A. Fluke 8846A Precision DMM

The Fluke 8846A is a highly versatile 6.5-digit multimeter. When configuring DC and AC modes, speed and noise are governed by two major parameters:

#### 1. NPLC (Number of Power Line Cycles)
*Applies to: DC Voltage, DC Current, 2-Wire Resistance, and 4-Wire Resistance.*

NPLC controls the integration time of the A/D converter in terms of power line cycles (60 Hz in North America = 16.67 ms per cycle; 50 Hz in Europe = 20 ms per cycle).

| NPLC Setting | Integration Time (60Hz) | Measurement Speed | Resolution & Noise | Appropriate Use Cases |
| :--- | :--- | :--- | :--- | :--- |
| **0.02** | ~333 μs | Extremely Fast (~3000 rdgs/s) | Low resolution, high noise | High-speed transient capture, signal tracking where line noise is negligible or filtered later. |
| **0.2** | ~3.33 ms | Fast (~300 rdgs/s) | Moderate noise, ~4.5 digits | General fast scouting, tracking rapid physical variations. |
| **1.0** *(Default)* | ~16.67 ms | Medium (~60 rdgs/s) | Clean, ~5.5 digits | Standard operation. Automatically rejects 60 Hz line noise. Best balance of speed/precision. |
| **10.0** | ~166.7 ms | Slow (~6 rdgs/s) | Extremely quiet, ~6.5 digits | Precision metrology, steady-state calibration, low-level signal measurements. |
| **100.0** | ~1.67 s | Very Slow (~0.6 rdgs/s) | Ultimate precision, ultra-low noise | Absolute state measurements where noise suppression is critical. |

#### 2. AC Filter Bandwidth
*Applies to: AC Voltage and AC Current.*

The AC filter bandwidth setting adjusts the time constant of the RMS converter to handle lower-frequency AC signals.

| AC Filter Option | Lower Cutoff Freq | Settling Time | Measurement Speed | Best Used For |
| :--- | :--- | :--- | :--- | :--- |
| **Fast (200 Hz)** | 200 Hz | Short | Fast (~50 rdgs/s) | High-frequency AC signals. Allows fast acquisition rates. |
| **Medium (20 Hz)** *(Default)*| 20 Hz | Medium | Medium (~4 rdgs/s) | Standard AC power or signal measurements down to 20 Hz. |
| **Slow (3 Hz)** | 3 Hz | Long (~7 seconds) | Very Slow | Low-frequency AC signals. *Warning: The GUI polling interval must be set to at least 2000–3000 ms to avoid visa timeout warnings.* |

---

### B. AH2550A Precision Capacitance Bridge

The Andeen-Hagerling AH2550A measures ultra-low capacitance with high stability. Its primary configuration parameter is the **Averaging Exponent (Avg Exp)**.

* **Formula**: The instrument averages $2^{\text{Avg Exp}}$ internal measurements per reading.

| Avg Exp Value | Approximate Speed | Noise Floor | Best Used For |
| :--- | :--- | :--- | :--- |
| **0 to 3** | Extremely Fast (< 0.1 s) | High (fF to pF range) | Rapid setup, verifying connections, finding cables. |
| **4 to 7** | Fast (0.1 s to 0.5 s) | Moderate | Coarse scanning, watching physical manipulation of samples in real time. |
| **8 to 10** *(Default)*| Moderate (1s to 2s) | Low (aF range) | General precision research. Good balance of wait time vs noise. |
| **11 to 13** | Slow (4s to 20s) | Extremely Low | Steady-state measurements, high-accuracy thermal sweeps. |
| **14 to 15** | Very Slow (1 min to 5 min) | Sub-attofarad (ultimate) | Calibrations, critical reference sweeps of stable physics samples. |

> [!WARNING]
> High Averaging Exponent values (12+) make measurements take a substantial amount of time. If you use an Averaging Exponent of 12 or higher, set the GUI **Interval (ms)** to a high value (e.g. `5000` to `20000`) or `0` to prevent unnecessary query accumulation.

---

### C. Keithley Electrometer (6517B) & Picoammeter (6485)

These instruments measure low-level currents, voltages, and charges. 

* **Available Modes**:
  * *DC Voltage* (Electrometer only)
  * *DC Current* (Electrometer & Picoammeter)
  * *Charge* (Electrometer only)
* **Speed Configuration**:
  * In the Fast Edition interface, these panels auto-range and run at the speed dictated by the GUI **Interval (ms)**.
  * To measure extremely low currents (picoamps/femtoamps), physical shielding and slow sampling rates (e.g., **Interval = 500 ms or 1000 ms**) are recommended to allow the input amplifiers to settle.
  * Zero Check (`:SYST:ZCH OFF`) is automatically disabled upon startup so that the instruments can measure continuously.

---

## 4. Quick-Reference Matrix: "What Should I Set?"

| Measurement Target | Instrument | Tab Selection | Suggested Settings | Why? |
| :--- | :--- | :--- | :--- | :--- |
| **Rapid current transients** | Keithley Picoammeter | `Picoammeter` | Interval: `0 ms` (or `10 ms`) | Maximize throughput to capture transient spikes. |
| **High-precision static capacitance** | AH2550A Bridge | `Capacitance Bridge` | Avg Exp: `10` or `11`, Interval: `0` | Ultra-low noise averaging over stable sample. |
| **Rapid heating/cooling cap sweep** | AH2550A Bridge | `Capacitance Bridge` | Avg Exp: `6` or `7`, Interval: `0` | Need faster readings to prevent temp lag. |
| **Fast digital signals (AC Volts)** | Fluke 8846A | `Fluke 8846A` | AC Filter: `Fast`, Interval: `10 ms` | Signal is high freq, priority is speed. |
| **Mains AC voltage stability** | Fluke 8846A | `Fluke 8846A` | AC Filter: `Medium`, Interval: `500 ms` | 60 Hz mains signal requires medium bandwidth filter. |
| **Low-resistance shunt (DC)** | Fluke 8846A | `Fluke 8846A` | Function: `Resistance (4-wire)`, NPLC: `10`, Interval: `200 ms` | 4-wire mode eliminates lead resistance; NPLC 10 filters noise. |
| **High-value insulator leakage** | Keithley Electrometer | `Electrometer` | Function: `DC Current`, Interval: `1000 ms` | Low currents require longer settling times. |

---

## 5. Troubleshooting & Status Codes

Look at the status bar at the bottom left of the window for real-time status diagnostics:

* <span style="color:#5cdd8b">■ **Green (OK)**</span>: The instrument is connected and communicating normally.
* <span style="color:#ffd66b">■ **Yellow (WARN)**</span>: Configuration command warning or a minor communication lag.
* <span style="color:#ff6b7a">■ **Red (ERR)**</span>: Connection failed, command rejected, or PyVISA could not locate the instrument address.
  * *Fix*: Click **↻ Refresh VISA** to update the list of available addresses, double-check GPIB cable connections, and verify that the instruments are powered on.
