# HF Doppler Analysis Tool

A real-time waterfall and peak-frequency tracking tool for HF Doppler / traveling
ionospheric disturbance (TID) studies, built around a FlexRadio DAX audio feed
(e.g. monitoring a CHU time-signal carrier).

## Overview

The tool captures audio from a FlexRadio DAX RX channel, computes a live FFT
waterfall display, and tracks the peak tone (the offset-tuned carrier) over
time. Peak frequency and magnitude are logged to CSV, and the waterfall image
is periodically saved to disk. A companion FlexRadio TCP client can subscribe
to radio state over the SmartSDR TCP API.

Typical use case: receive a known-frequency reference signal (e.g. CHU on
7850 kHz, tuned 1 kHz high in USB so the carrier appears as a tone at the
expected audio offset), and track Doppler shift of that tone over time to
observe ionospheric TIDs.

## Features

- Live scrolling waterfall display (PyQtGraph) with time-labeled Y axis and
  frequency-labeled X axis
- Configurable FFT size, decimation factor, LPF taps/cutoff, and sample
  attenuation via a **Processing Settings** dialog
- Automatic peak-tone detection with a live **Peak Info** dialog (frequency,
  magnitude, timestamp)
- Peak records logged continuously to CSV and buffered in memory for a
  final combined CSV + matplotlib plot on exit
- Periodic waterfall PNG snapshots saved to disk
- FlexRadio TCP client (`TCP_Flex.py` / `TCP_Flex2.py`) for a persistent,
  threaded connection to the SmartSDR TCP (CAT) API
- Graceful shutdown: stops audio capture, flushes buffered samples, and
  writes final CSV/PNG outputs

## Requirements

- Python 3.x
- [PyQtGraph](https://www.pyqtgraph.org/) (and its Qt binding, e.g. PySide/PyQt)
- `sounddevice`
- `numpy`
- `pandas`
- `matplotlib`
- A FlexRadio with SmartSDR and **DAX RX 1** enabled, receiver tuned in USB
  with the carrier offset per the convention above
- Windows (audio device discovery prioritizes WASAPI / WDM-KS / DirectSound /
  MME host APIs, in that order)

No `requirements.txt` is currently checked in; install the packages above
into your environment (e.g. a venv) before running.

## Usage

```
python HFDoppTool.py
```

- The main window opens with the live waterfall plot.
- **Processing Settings...** opens a dialog to set TX/RX station labels,
  radio frequency (reference only today — see below), sample rate, FFT size,
  decimation factor, sample attenuation, LPF taps/cutoff, and CSV
  filename/directory.
- **Show Peak Dialog** opens a non-modal window showing the current peak
  frequency, magnitude, and timestamp, updated on the same 30 ms timer as the
  waterfall.
- On exit, the app stops the audio stream, saves the final waterfall PNG,
  writes the accumulated peak-frequency CSV, and shows a combined
  frequency/magnitude plot before saving it as a PNG.

## Configuration

Key defaults live in `HFDOPP_audio_core.py` and are wrapped in `AppState`,
which is mutated at runtime via the Processing Settings dialog
(`apply_runtime_options`) and `apply_processing_config`:

| Constant | Purpose |
|---|---|
| `TARGET_AUDIO_OUT_NAME` | Substring used to match the FlexRadio DAX input device (`"DAX RX 1 (FlexRadio Systems"`) |
| `PREFERRED_DEVICE_INDEX` | Fallback sounddevice index if name matching fails |
| `DEFAULT_SAMPLE_RATE` / `DEFAULT_FFT_SIZE` / `DEFAULT_DECIMATION_FACTOR` | DSP defaults |
| `MIN_FFT_SIZE` / `MAX_FFT_SIZE` | Allowed FFT size range (must be a power of two) |
| `WATERFALL_HEIGHT` | Number of scrolling rows kept in the waterfall buffer |
| `MIN_DB_DEFAULT` / `MAX_DB_DEFAULT` | Initial color-scale range for the waterfall |
| `CSV_FILENAME` / `CSV_DIRECTORY` | Default output CSV name and directory |

## File layout

- [HFDoppTool.py](HFDoppTool.py) — main application: Qt UI, waterfall
  rendering, peak detection, audio callback wiring, and shutdown/save logic
- [HFDOPP_audio_core.py](HFDOPP_audio_core.py) — `AppState` dataclass, DSP
  parameter derivation, FIR low-pass filter design, CSV record appending
- [HFDOPP_audio_devices.py](HFDOPP_audio_devices.py) — sounddevice input
  device discovery/matching and stream startup
- [TCP_Flex.py](TCP_Flex.py) — standalone interactive script for a
  SmartSDR TCP session (hardcoded host/port, manual send loop)
- [TCP_Flex2.py](TCP_Flex2.py) — reusable threaded telnet-style TCP client
  (`start_telnet_client`) used for radio subscription/control
- [post_process_tools.py](post_process_tools.py) — offline script to reload
  a saved peak-frequency CSV and regenerate the frequency/magnitude plot

## Output data

- **Waterfall images**: `<station>_<UTC timestamp>_waterfall_<seq>.png` in
  the configured results directory, saved periodically and on shutdown.
- **Peak frequency CSV**: appended continuously as
  `timestamp,peak_freq_hz,peak_mag_db`, plus a final combined CSV named
  `<station>_<UTC timestamp>_df_FreqMag.csv` written on shutdown.
- `<station>` is `tx_station` alone, or `tx_station_rx_station` when a
  receiver station is set.

## FlexRadio TCP integration status

Currently the TCP client only opens a connection and subscribes to
panadapter state (`sub pan all`) for read/monitoring purposes — it does not
yet control the radio. The **Radio Frequency** field in Processing Settings
is presently a manually-entered reference value (`STATE.radio_frequency_khz`)
and is not sent to the radio.

Planned: issue an outbound TCP command to tune the radio to
`radio_frequency_khz` when Processing Settings are applied.

## Known limitations

- Default results directory (`CSV_DIRECTORY`) is a hardcoded local Windows
  path.
- Assumes a single audio channel (DAX RX 1) and USB mode with a fixed
  carrier offset convention.
- Windows-oriented audio host API handling (WASAPI/WDM-KS/DirectSound/MME).
