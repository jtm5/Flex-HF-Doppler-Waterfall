
####################################################################
# HF Doppler Analysis Tool
# Main application entry point and supporting functions for
# managing audio capture, waterfall image saving, and peak
# frequency/magnitude data collection and persistence.

# Assume FlexRadio DAX RX 1 device for audio input (as configured in TARGET_AUDIO_OUT_NAME)
# Assumes USB mode with the carrier tuned 1 khz high so as to produce a tone at the expected frequency offset.
# Note the DAX channel nomenclature was updated in SmartSDR v4, dropping the "Audio" part of the name.
####################################################################




import os
from pyqtgraph.Qt import QtWidgets, QtCore
import pyqtgraph as pg
from pyqtgraph import exporters
import sounddevice as sd
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime, timezone
from queue import Empty, Queue
from TCP_Flex2 import start_telnet_client


from HFDOPP_audio_core import (
    AppState,
    CSV_DIRECTORY,
    CSV_FILENAME,
    DEFAULT_DECIMATION_FACTOR,
    INPUT_CHANNEL_INDEX,
    LABEL_PIXEL_SPACING,
    MAG_EPSILON,
    MAX_DB_DEFAULT,
    MAX_FFT_SIZE,
    MIN_DB_DEFAULT,
    MIN_FFT_SIZE,
    PREFERRED_DEVICE_INDEX,
    TARGET_AUDIO_OUT_NAME,
    WATERFALL_HEIGHT,
    apply_processing_config,
    apply_runtime_options,
    append_peak_record,
    valid_fft_sizes,
)
from HFDOPP_audio_devices import (
    find_target_input_devices,
    get_hostapi_name,
    hostapi_priority,
    start_audio_stream,
)


STATE = AppState()

START_DATETIME = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
print(f"Starting HF Doppler Analysis Tool at {START_DATETIME}")


def _station_prefix():
    if STATE.rx_station:
        return f"{STATE.tx_station}_{STATE.rx_station}"
    return STATE.tx_station


def get_df_csv_basename():
    return f"{_station_prefix()}_{START_DATETIME.replace(' ', '_').replace(':', '')}_df_FreqMag.csv"




if DEFAULT_DECIMATION_FACTOR < 1:
    raise ValueError("DECIMATION_FACTOR must be >= 1")

# set up a dataframe global to store the peak frequency records in memory for quick access and later saving to CSV
peak_records_df = pd.DataFrame(columns=["timestamp", "peak_freq_hz", "peak_mag_db"])

data_queue = Queue()
cleanup_done = False


def waterfall_image_filename():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    return f"{_station_prefix()}_{timestamp}_waterfall_{STATE.waterfall_image_save_count:04d}.png"


def save_waterfall_image():
    os.makedirs(STATE.results_directory, exist_ok=True)
    image_path = os.path.join(STATE.results_directory, waterfall_image_filename())

    exporter = exporters.ImageExporter(plot)
    exporter.export(image_path)

    STATE.waterfall_image_save_count += 1
    print(f"Saved waterfall image: {image_path}")

def clean_up_and_save_data(): # called when qt app ready to quit
    global peak_records_df, cleanup_done

    if cleanup_done:
        return
    cleanup_done = True

    # Stop periodic updates and audio capture before final data flush.
    try:
        if "timer" in globals() and timer.isActive():
            timer.stop()
    except Exception as exc:
        print(f"Warning: unable to stop update timer during cleanup: {exc}")

    try:
        if STATE.stream is not None:
            STATE.stream.stop()
            STATE.stream.close()
            STATE.stream = None
    except Exception as exc:
        print(f"Warning: unable to stop audio stream during cleanup: {exc}")

    try:
        if STATE.radio_stop is not None:
            STATE.radio_stop()
            STATE.radio_send = None
            STATE.radio_stop = None
    except Exception as exc:
        print(f"Warning: unable to close radio connection during cleanup: {exc}")

    # Process any final buffered audio that arrived before stream shutdown.
    try:
        update_waterfall()
    except Exception as exc:
        print(f"Warning: unable to process final buffered samples: {exc}")

    # Ensure any final queued samples are captured before writing outputs.
    drain_peak_data_queue()

    # Always persist the current waterfall view on shutdown so partial captures
    # are retained even when WATERFALL_HEIGHT has not been reached.
    try:
        if STATE.waterfall_lines_saved > 0:
            save_waterfall_image()
    except Exception as exc:
        print(f"Warning: unable to save final waterfall image during cleanup: {exc}")




    # convert timestamp strings to datetime objects for better sorting and analysis
    peak_records_df["timestamp"] = pd.to_datetime(peak_records_df["timestamp"], errors="coerce")

    print("dataframe", peak_records_df)
    os.makedirs(STATE.results_directory, exist_ok=True)
    df_csv_filename = os.path.join(STATE.results_directory, get_df_csv_basename())
    peak_records_df.to_csv(df_csv_filename, index=False)
    print(f"Saved {len(peak_records_df)} records to CSV: {df_csv_filename}")


    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    ax1.set_ylabel("Peak Frequency (Hz)", color="tab:blue")
    ax1.plot(peak_records_df["timestamp"], peak_records_df["peak_freq_hz"], color="tab:blue")
    ax1.set_ylim(998.0,1002.0)

    ax2.set_xlabel("Time")
    ax2.set_ylabel("Peak Magnitude (dB)", color="tab:red")
    ax2.plot(peak_records_df["timestamp"], peak_records_df["peak_mag_db"], color="tab:red")

    plt.savefig(os.path.join(STATE.results_directory, f"{_station_prefix()}_{START_DATETIME.replace(' ', '_').replace(':', '')}_df_FreqMag.png"))
    plt.show()
    plt.close("all")
    print(f"Saved peak frequency and magnitude plot: {_station_prefix()}_{START_DATETIME.replace(' ', '_').replace(':', '')}_df_FreqMag.png")
   

def drain_peak_data_queue():
    global peak_records_df

    while True:
        try:
            timestamp, peak_freq, peak_mag = data_queue.get_nowait()
        except Empty:
            break

        peak_records_df.loc[len(peak_records_df)] = {
            "timestamp": timestamp,
            "peak_freq_hz": peak_freq,
            "peak_mag_db": peak_mag,
        }


# -------------------------------
# Qt Application
# -------------------------------
app = QtWidgets.QApplication([])


class PeakInfoDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Current Peak")
        self.setModal(False)

        layout = QtWidgets.QFormLayout(self)
        self.freq_label = QtWidgets.QLabel("-")
        self.mag_label = QtWidgets.QLabel("-")
        self.time_label = QtWidgets.QLabel("-")

        layout.addRow("Peak Frequency:", self.freq_label)
        layout.addRow("Peak Magnitude:", self.mag_label)
        layout.addRow("Timestamp:", self.time_label)

    def update_peak(self, peak_freq_hz, peak_mag_db, timestamp):
        self.freq_label.setText(f"{peak_freq_hz:.4f} Hz")
        self.mag_label.setText(f"{peak_mag_db:.2f} dB")
        self.time_label.setText(timestamp)


class ProcessingSettingsDialog(QtWidgets.QDialog):
    def __init__(self, sample_rate, fft_size, decimation_factor, attenuation,
                 lpf_tap_count, lpf_cutoff_hz, csv_filename, csv_directory,
                 tx_station="", rx_station="", radio_frequency_mhz=0.0,
                 radio_host="", radio_port=4992,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Processing Settings")

        layout = QtWidgets.QFormLayout(self)

        self.tx_station_edit = QtWidgets.QLineEdit(tx_station)
        self.radio_frequency_spin = QtWidgets.QDoubleSpinBox()
        self.radio_frequency_spin.setRange(0.0, 30000.0)
        self.radio_frequency_spin.setDecimals(3)
        self.radio_frequency_spin.setSingleStep(1.0)
        self.radio_frequency_spin.setSuffix(" MHz")
        self.radio_frequency_spin.setValue(float(radio_frequency_mhz))
        self.rx_station_edit = QtWidgets.QLineEdit(rx_station)
        self.radio_host_edit = QtWidgets.QLineEdit(radio_host)
        self.radio_port_spin = QtWidgets.QSpinBox()
        self.radio_port_spin.setRange(1, 65535)
        self.radio_port_spin.setValue(int(radio_port))
        layout.addRow("Transmitter Station:", self.tx_station_edit)
        layout.addRow("Radio Frequency:", self.radio_frequency_spin)
        layout.addRow("Receiver Station:", self.rx_station_edit)
        layout.addRow("Radio IP Address:", self.radio_host_edit)
        layout.addRow("Radio TCP Port:", self.radio_port_spin)

        self.sample_rate_spin = QtWidgets.QSpinBox()
        self.sample_rate_spin.setRange(8000, 384000)
        self.sample_rate_spin.setSingleStep(1000)
        self.sample_rate_spin.setValue(int(sample_rate))

        self.fft_sizes = valid_fft_sizes(MIN_FFT_SIZE, MAX_FFT_SIZE)
        self.fft_combo = QtWidgets.QComboBox()
        self.fft_combo.setEditable(False)
        for size in self.fft_sizes:
            self.fft_combo.addItem(str(size), size)

        fft_size_int = int(fft_size)
        if fft_size_int not in self.fft_sizes:
            fft_size_int = min(self.fft_sizes, key=lambda s: abs(s - fft_size_int))
        self.fft_combo.setCurrentText(str(fft_size_int))

        self.decimation_spin = QtWidgets.QSpinBox()
        self.decimation_spin.setRange(1, 128)
        self.decimation_spin.setValue(int(decimation_factor))

        self.attenuation_spin = QtWidgets.QDoubleSpinBox()
        self.attenuation_spin.setRange(0.0001, 1.0)
        self.attenuation_spin.setDecimals(4)
        self.attenuation_spin.setSingleStep(0.01)
        self.attenuation_spin.setValue(float(attenuation))

        self.lpf_taps_spin = QtWidgets.QSpinBox()
        self.lpf_taps_spin.setRange(15, 511)
        self.lpf_taps_spin.setSingleStep(2)
        self.lpf_taps_spin.setValue(int(lpf_tap_count))

        self.lpf_cutoff_spin = QtWidgets.QDoubleSpinBox()
        self.lpf_cutoff_spin.setDecimals(1)
        self.lpf_cutoff_spin.setSingleStep(10.0)
        self.lpf_cutoff_spin.setRange(10.0, max(10.0, sample_rate * 0.499))
        self.lpf_cutoff_spin.setValue(float(lpf_cutoff_hz))

        self.csv_filename_edit = QtWidgets.QLineEdit(csv_filename)
        self.csv_directory_edit = QtWidgets.QLineEdit(csv_directory)
        self.csv_browse_button = QtWidgets.QPushButton("Browse...")
        self.csv_browse_button.clicked.connect(self._select_csv_directory)
        csv_dir_layout = QtWidgets.QHBoxLayout()
        csv_dir_layout.addWidget(self.csv_directory_edit)
        csv_dir_layout.addWidget(self.csv_browse_button)
        csv_dir_widget = QtWidgets.QWidget()
        csv_dir_widget.setLayout(csv_dir_layout)

        self.sample_rate_spin.valueChanged.connect(self._on_sample_rate_changed)

        layout.addRow("Sample rate (Hz):", self.sample_rate_spin)
        layout.addRow("FFT size:", self.fft_combo)
        layout.addRow("Decimation factor:", self.decimation_spin)
        layout.addRow("Sample attenuation:", self.attenuation_spin)
        layout.addRow("LPF taps (odd):", self.lpf_taps_spin)
        layout.addRow("LPF cutoff (Hz):", self.lpf_cutoff_spin)
        layout.addRow("CSV filename:", self.csv_filename_edit)
        layout.addRow("CSV directory:", csv_dir_widget)

        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addRow(button_box)

    def get_values(self):
        idx = self.fft_combo.currentIndex()
        if idx < 0 or idx >= len(self.fft_sizes):
            idx = 0
        fft_size = int(self.fft_sizes[idx])
        return {
            "tx_station": self.tx_station_edit.text().strip(),
            "radio_frequency_khz": float(self.radio_frequency_spin.value()),
            "rx_station": self.rx_station_edit.text().strip(),
            "radio_host": self.radio_host_edit.text().strip(),
            "radio_port": int(self.radio_port_spin.value()),
            "sample_rate": int(self.sample_rate_spin.value()),
            "fft_size": fft_size,
            "decimation_factor": int(self.decimation_spin.value()),
            "attenuation": float(self.attenuation_spin.value()),
            "lpf_tap_count": int(self.lpf_taps_spin.value()),
            "lpf_cutoff_hz": float(self.lpf_cutoff_spin.value()),
            "csv_filename": self.csv_filename_edit.text().strip(),
            "csv_directory": self.csv_directory_edit.text().strip(),
        }

    def _on_sample_rate_changed(self, sample_rate):
        max_cutoff = max(10.0, float(sample_rate) * 0.499)
        self.lpf_cutoff_spin.setMaximum(max_cutoff)
        if self.lpf_cutoff_spin.value() > max_cutoff:
            self.lpf_cutoff_spin.setValue(max_cutoff)

    def _select_csv_directory(self):
        selected_dir = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select CSV Directory",
            self.csv_directory_edit.text().strip() or os.getcwd(),
        )
        if selected_dir:
            self.csv_directory_edit.setText(selected_dir)


apply_processing_config(STATE, STATE.sample_rate, STATE.fft_size, STATE.decimation_factor)


class WaterfallTimeAxis(pg.AxisItem):
    def __init__(self, state, orientation="left"):
        super().__init__(orientation=orientation)
        self._state = state

    def tickStrings(self, values, scale, spacing):
        labels = []
        timestamp_rows = self._state.waterfall_row_timestamps
        max_index = len(timestamp_rows) - 1
        for value in values:
            # Map displayed y position to the mirrored row index when Y is inverted.
            index = max_index - int(round(value))
            if 0 <= index <= max_index:
                labels.append(str(timestamp_rows[index]))
            else:
                labels.append("")
        return labels

main_win = QtWidgets.QWidget()
main_win.setWindowTitle("K1FR HF Doppler Analysis Tool")
main_layout = QtWidgets.QVBoxLayout(main_win)

win = pg.GraphicsLayoutWidget()
main_layout.addWidget(win)

left_axis = WaterfallTimeAxis(STATE)
plot = win.addPlot(axisItems={"left": left_axis})
img = pg.ImageItem()
plot.addItem(img)
plot.setAspectLocked(False)
plot.invertY(True)
plot.setLabel("left", "Time (HH:mm:ss)")
plot.setLabel("bottom", "Frequency (Hz)")

bottom_axis = plot.getAxis("bottom")


def nice_tick_step(range_hz, target_ticks=11):
    if range_hz <= 0:
        return 1.0

    rough_step = range_hz / max(2, target_ticks - 1)
    exponent = np.floor(np.log10(rough_step))
    magnitude = 10 ** exponent
    normalized = rough_step / magnitude

    if normalized <= 1:
        nice = 1
    elif normalized <= 2:
        nice = 2
    elif normalized <= 2.5:
        nice = 2.5
    elif normalized <= 5:
        nice = 5
    else:
        nice = 10

    return float(nice * magnitude)


def decimals_for_step(step_value, max_decimals=4):
    step_value = max(float(step_value), 1e-12)
    text = f"{step_value:.{max_decimals}f}".rstrip("0").rstrip(".")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def format_freq_label(freq_hz, step_hz):
    if STATE.nyquist_hz >= 1000:
        step_khz = max(step_hz / 1000.0, 1e-12)
        decimals = decimals_for_step(step_khz)
        return f"{freq_hz / 1000:.{decimals}f} kHz"

    decimals = decimals_for_step(step_hz)
    return f"{freq_hz:.{decimals}f} Hz"


def target_major_tick_count():
    # Keep labels legible by targeting one major label roughly every N pixels.
    view_width_px = max(1.0, float(plot.vb.width()))
    return int(np.clip(np.floor(view_width_px / LABEL_PIXEL_SPACING), 4, 16))


def update_x_axis_ticks(low_hz, high_hz):
    visible = max(1.0, high_hz - low_hz)
    step = nice_tick_step(visible, target_ticks=target_major_tick_count())
    first_idx = int(np.ceil(low_hz / step))
    last_idx = int(np.floor(high_hz / step))
    tick_indices = np.arange(first_idx, last_idx + 1)
    tick_positions = tick_indices * step

    major_ticks = [
        (float(pos), format_freq_label(float(pos), step))
        for pos in tick_positions
        if 0 <= pos <= STATE.nyquist_hz
    ]

    if not major_ticks:
        major_ticks = [(float(low_hz), format_freq_label(float(low_hz), step))]

    # Add minor tick marks between major ticks for better zoomed readability.
    minor_divisions = 5
    minor_step = step / minor_divisions
    first_minor_idx = int(np.ceil(low_hz / minor_step))
    last_minor_idx = int(np.floor(high_hz / minor_step))
    minor_indices = np.arange(first_minor_idx, last_minor_idx + 1)
    minor_positions = minor_indices * minor_step

    major_values = np.array([tick[0] for tick in major_ticks], dtype=np.float64)
    minor_ticks = []
    for pos in minor_positions:
        if not (0 <= pos <= STATE.nyquist_hz):
            continue
        if major_values.size and np.any(np.isclose(pos, major_values, atol=minor_step * 0.1)):
            continue
        minor_ticks.append((float(pos), ""))

    bottom_axis.setTicks([major_ticks, minor_ticks])


def on_plot_resized():
    update_frequency_view()

lut = pg.colormap.get("inferno").getLookupTable()

min_db = MIN_DB_DEFAULT
max_db = MAX_DB_DEFAULT
center_hz = 1000
span_hz = STATE.nyquist_hz

min_db_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
min_db_slider.setRange(-160, 0)
min_db_slider.setValue(min_db)
min_db_slider.setToolTip("Waterfall minimum dB")

max_db_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
max_db_slider.setRange(-60, 20)
max_db_slider.setValue(max_db)
max_db_slider.setToolTip("Waterfall maximum dB")

min_db_label = QtWidgets.QLabel(f"Min dB: {min_db}")
max_db_label = QtWidgets.QLabel(f"Max dB: {max_db}")

center_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
center_slider.setRange(0, int(STATE.nyquist_hz))
center_slider.setValue(center_hz)
center_slider.setToolTip("Center frequency shown on X-axis")

span_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
span_slider.setRange(0, 1000)
span_slider.setValue(1000)
span_slider.setToolTip("Visible frequency span")

center_label = QtWidgets.QLabel(f"Center: {center_hz} Hz")
span_label = QtWidgets.QLabel(f"Span: {span_hz} Hz")
processing_status_label = QtWidgets.QLabel()


def span_to_slider_value(span):
    log_min = np.log10(STATE.min_span_hz)
    log_max = np.log10(STATE.nyquist_hz)
    if np.isclose(log_min, log_max):
        return 1000
    normalized = (np.log10(span) - log_min) / (log_max - log_min)
    return int(np.clip(normalized * 1000, 0, 1000))


def slider_value_to_span(value):
    log_min = np.log10(STATE.min_span_hz)
    log_max = np.log10(STATE.nyquist_hz)
    if np.isclose(log_min, log_max):
        return int(round(STATE.nyquist_hz))
    log_span = log_min + (value / 1000.0) * (log_max - log_min)
    return int(round(10 ** log_span))


def update_frequency_view():
    half = span_hz / 2
    low = max(0, center_hz - half)
    high = min(STATE.nyquist_hz, center_hz + half)

    current_span = high - low
    if current_span < span_hz:
        deficit = span_hz - current_span
        if low <= 0:
            high = min(STATE.nyquist_hz, high + deficit)
        elif high >= STATE.nyquist_hz:
            low = max(0, low - deficit)

    plot.setXRange(low, high, padding=0)
    update_x_axis_ticks(low, high)


def on_min_db_changed(value):
    global min_db, max_db
    if value >= max_db:
        value = max_db - 1
        min_db_slider.blockSignals(True)
        min_db_slider.setValue(value)
        min_db_slider.blockSignals(False)
    min_db = value
    min_db_label.setText(f"Min dB: {min_db}")


def on_max_db_changed(value):
    global min_db, max_db
    if value <= min_db:
        value = min_db + 1
        max_db_slider.blockSignals(True)
        max_db_slider.setValue(value)
        max_db_slider.blockSignals(False)
    max_db = value
    max_db_label.setText(f"Max dB: {max_db}")


def on_center_changed(value):
    global center_hz
    center_hz = value
    center_label.setText(f"Center: {center_hz} Hz")
    update_frequency_view()


def on_span_changed(value):
    global span_hz
    span_hz = slider_value_to_span(value)
    span_label.setText(f"Span: {span_hz} Hz")
    update_frequency_view()


def set_image_rect_safe(width_hz, height_rows):
    image_data = getattr(img, "image", None)
    if image_data is None:
        return

    try:
        width_hz = float(width_hz)
        height_rows = float(height_rows)
    except (TypeError, ValueError):
        return

    if not np.isfinite(width_hz) or width_hz <= 0:
        return
    if not np.isfinite(height_rows) or height_rows <= 0:
        return

    try:
        img.setRect(QtCore.QRectF(0.0, 0.0, width_hz, height_rows))
    except Exception as exc:
        print(f"Warning: unable to update image rect: {exc}")


def sync_ui_with_processing_config():
    global center_hz, span_hz

    nyquist = float(STATE.nyquist_hz)
    if not np.isfinite(nyquist) or nyquist <= 0:
        nyquist = 1.0

    min_span = float(STATE.min_span_hz)
    if not np.isfinite(min_span) or min_span <= 0:
        min_span = 1.0

    center_hz = int(np.clip(center_hz, 0, nyquist))
    span_hz = int(np.clip(span_hz, min_span, nyquist))

    center_slider.blockSignals(True)
    center_slider.setRange(0, int(nyquist))
    center_slider.setValue(center_hz)
    center_slider.blockSignals(False)

    span_slider.blockSignals(True)
    span_slider.setValue(span_to_slider_value(span_hz))
    span_slider.blockSignals(False)

    center_label.setText(f"Center: {center_hz} Hz")
    span_label.setText(f"Span: {span_hz} Hz")
    processing_status_label.setText(
        f"Active FFT: {STATE.fft_size} | Blocksize: {STATE.input_samples_per_fft}"
    )
    set_image_rect_safe(nyquist, WATERFALL_HEIGHT)
    update_frequency_view()


def handle_radio_message(msg):
    print(f"[RADIO] {msg}")


def handle_radio_disconnect():
    print("Radio TCP connection closed.")
    STATE.radio_send = None
    STATE.radio_stop = None


def reconnect_radio_if_needed(old_host, old_port):
    """(Re)connect to the FlexRadio TCP API if not connected or the address changed."""
    if STATE.radio_send is not None and STATE.radio_host == old_host and STATE.radio_port == old_port:
        return

    if STATE.radio_stop is not None:
        try:
            STATE.radio_stop()
        except Exception as exc:
            print(f"Warning: unable to close previous radio connection: {exc}")
        STATE.radio_send = None
        STATE.radio_stop = None

    try:
        STATE.radio_send, STATE.radio_stop = start_telnet_client(
            host=STATE.radio_host,
            port=STATE.radio_port,
            on_message=handle_radio_message,
            on_disconnect=handle_radio_disconnect,
        )
    except Exception as exc:
        STATE.radio_send = None
        STATE.radio_stop = None
        QtWidgets.QMessageBox.warning(
            main_win,
            "Radio Connection Failed",
            f"Unable to connect to radio at {STATE.radio_host}:{STATE.radio_port}:\n{exc}",
        )


def open_processing_settings_dialog(_checked=False, *, restart_stream=True):
    dialog = ProcessingSettingsDialog(
        sample_rate=STATE.sample_rate,
        fft_size=STATE.fft_size,
        decimation_factor=STATE.decimation_factor,
        attenuation=STATE.sample_attenuation,
        lpf_tap_count=STATE.lpf_tap_count,
        lpf_cutoff_hz=STATE.lpf_cutoff_hz,
        csv_filename=STATE.csv_filename,
        csv_directory=STATE.results_directory,
        tx_station=STATE.tx_station,
        rx_station=STATE.rx_station,
        radio_frequency_mhz=STATE.radio_frequency_khz,
        radio_host=STATE.radio_host,
        radio_port=STATE.radio_port,
        parent=main_win,
    )

    if dialog.exec_() != QtWidgets.QDialog.Accepted:
        return

    old_values = {
        "sample_rate": STATE.sample_rate,
        "fft_size": STATE.fft_size,
        "decimation_factor": STATE.decimation_factor,
        "attenuation": STATE.sample_attenuation,
        "lpf_tap_count": STATE.lpf_tap_count,
        "lpf_cutoff_hz": STATE.lpf_cutoff_hz,
        "csv_filename": STATE.csv_filename,
        "csv_directory": STATE.results_directory,
    }
    old_radio_host = STATE.radio_host
    old_radio_port = STATE.radio_port

    values = dialog.get_values()
    if not values["csv_filename"]:
        QtWidgets.QMessageBox.warning(main_win, "Invalid CSV", "CSV filename cannot be empty.")
        return
    if not values["csv_directory"]:
        QtWidgets.QMessageBox.warning(main_win, "Invalid CSV", "CSV directory cannot be empty.")
        return

    try:
        apply_processing_config(
            STATE,
            values["sample_rate"],
            values["fft_size"],
            values["decimation_factor"],
            values["lpf_tap_count"],
            values["lpf_cutoff_hz"],
        )
        apply_runtime_options(STATE, values)

        # Connect (or reconnect, if the address changed) to the FlexRadio and initialize it.
        reconnect_radio_if_needed(old_radio_host, old_radio_port)
        if STATE.radio_send is not None:
            STATE.radio_send("c1| sub pan all")
            # this waterfall version does not use I/Q data, so the DAX IQ command is not needed.
            # STATE.radio_send("c11|dax iq set 1 pan 0x40000000  daxiq_rate=48000")
            STATE.radio_send(f"c2|display pan s 0x40000000 center={STATE.radio_frequency_khz}")
            STATE.radio_send(f"c3|slice tune 0 {STATE.radio_frequency_khz}")
            STATE.radio_send("c4|slice set 0 mode=USB")
            STATE.radio_send("c5|slice set 0 dax=1")

        main_win.setWindowTitle(f"K1FR HF Doppler Analysis Tool  —  TX: {STATE.tx_station}  RX: {STATE.rx_station}")
        sync_ui_with_processing_config()
    except Exception as exc:
        # Roll back only when settings themselves are invalid or cannot be applied.
        apply_processing_config(
            STATE,
            old_values["sample_rate"],
            old_values["fft_size"],
            old_values["decimation_factor"],
            old_values["lpf_tap_count"],
            old_values["lpf_cutoff_hz"],
        )
        apply_runtime_options(STATE, old_values)
        sync_ui_with_processing_config()
        if restart_stream:
            start_audio_stream(STATE, audio_callback)
        QtWidgets.QMessageBox.critical(
            main_win,
            "Settings Not Applied",
            f"Unable to apply settings:\n{exc}",
        )
        return

    if not restart_stream:
        return

    try:
        start_audio_stream(STATE, audio_callback)
    except Exception as exc:
        QtWidgets.QMessageBox.warning(
            main_win,
            "Stream Restart Failed",
            (
                "Settings were applied, but audio stream restart failed.\n"
                f"Current settings remain active.\n\nDetails:\n{exc}"
            ),
        )


min_db_slider.valueChanged.connect(on_min_db_changed)
max_db_slider.valueChanged.connect(on_max_db_changed)
center_slider.valueChanged.connect(on_center_changed)
span_slider.valueChanged.connect(on_span_changed)
span_slider.setValue(span_to_slider_value(span_hz))

controls_widget = QtWidgets.QWidget()
controls_layout = QtWidgets.QGridLayout(controls_widget)
show_peak_button = QtWidgets.QPushButton("Show Peak Dialog")
settings_button = QtWidgets.QPushButton("Processing Settings...")
controls_layout.addWidget(min_db_label, 0, 0)
controls_layout.addWidget(min_db_slider, 0, 1)
controls_layout.addWidget(max_db_label, 1, 0)
controls_layout.addWidget(max_db_slider, 1, 1)
controls_layout.addWidget(center_label, 2, 0)
controls_layout.addWidget(center_slider, 2, 1)
controls_layout.addWidget(span_label, 3, 0)
controls_layout.addWidget(span_slider, 3, 1)
controls_layout.addWidget(show_peak_button, 4, 0)
controls_layout.addWidget(settings_button, 4, 1)
controls_layout.addWidget(processing_status_label, 5, 0, 1, 2)
main_layout.addWidget(controls_widget)

peak_dialog = PeakInfoDialog(main_win)
show_peak_button.clicked.connect(peak_dialog.show)
settings_button.clicked.connect(open_processing_settings_dialog)
peak_dialog.show()

main_win.resize(1100, 700)
main_win.show()
plot.vb.sigResized.connect(on_plot_resized)
sync_ui_with_processing_config()
update_frequency_view()


# -------------------------------
# Waterfall Update (GUI thread)
# -------------------------------
def update_waterfall():
    while not STATE.waterfall_queue.empty():
        samples = STATE.waterfall_queue.get()

        samples = (samples * STATE.sample_attenuation).astype(np.float32, copy=False)

        padded = np.concatenate((STATE.lpf_state, samples))
        filtered = np.convolve(padded, STATE.decim_lpf_taps, mode="valid")
        STATE.lpf_state = padded[-(STATE.lpf_tap_count - 1):]

        decimated_chunk = filtered[::STATE.decimation_factor]
        if decimated_chunk.size == 0:
            continue

        STATE.decimated_buffer = np.concatenate((STATE.decimated_buffer, decimated_chunk.astype(np.float32, copy=False)))

    while len(STATE.decimated_buffer) >= STATE.fft_size:
        block = STATE.decimated_buffer[:STATE.fft_size]
        STATE.decimated_buffer = STATE.decimated_buffer[STATE.fft_size:]

        spec = np.fft.rfft(block * STATE.window)
        mag = 20 * np.log10(np.abs(spec) + MAG_EPSILON)

        peak_bin = np.argmax(mag)
        peak_freq = peak_bin * STATE.effective_sample_rate / STATE.fft_size
        peak_mag = mag[peak_bin]
        timestamp = QtCore.QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss 'UTC'")
        peak_dialog.update_peak(peak_freq, peak_mag, timestamp)

        # print(f"{timestamp} - {peak_freq:.4f} Hz with magnitude {peak_mag:.2f} dB")

        data_queue.put((timestamp, peak_freq, peak_mag))

        append_peak_record(STATE, timestamp, peak_freq, peak_mag)

        STATE.waterfall = np.roll(STATE.waterfall, 1, axis=0)
        STATE.waterfall[-1, :] = mag
        STATE.waterfall_row_timestamps.append(
            QtCore.QDateTime.currentDateTimeUtc().toString("HH:mm:ss")
        )
        img.setImage(STATE.waterfall.T, lut=lut, autoLevels=False,
                     levels=(min_db, max_db))
        set_image_rect_safe(STATE.nyquist_hz, WATERFALL_HEIGHT)
        left_axis.picture = None
        left_axis.update()
        STATE.waterfall_lines_saved += 1
        if STATE.waterfall_lines_saved % WATERFALL_HEIGHT == 0:
            try:
                save_waterfall_image()
            except Exception as exc:
                print(f"Warning: unable to save waterfall image: {exc}")

 


    # Drain queued peaks on the GUI thread so DataFrame updates stay in one place.
    drain_peak_data_queue()


# -------------------------------
# Audio Callback (PortAudio thread)
# -------------------------------
def audio_callback(indata, frames, time, status):
    del frames, time
    if status:
        print("Status:", status)

    if indata.ndim != 2 or indata.shape[1] == 0:
        return

    channel = min(INPUT_CHANNEL_INDEX, indata.shape[1] - 1)
    audio = indata[:, channel].copy()

    STATE.waterfall_queue.put(audio)


# -------------------------------
# Find DAX RX 1 Input Device(s)
# -------------------------------
devices = sd.query_devices()
STATE.candidate_indices = find_target_input_devices(
    devices,
    preferred_device_index=PREFERRED_DEVICE_INDEX,
    target_audio_out_name=TARGET_AUDIO_OUT_NAME,
)

if not STATE.candidate_indices:
    print("Available input devices:")
    for idx, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            print(f"  {idx}: {dev['name']}")
    raise RuntimeError("FlexRadio DAX RX 1 input device not found")


STATE.candidate_indices = sorted(
    STATE.candidate_indices,
    key=lambda i: hostapi_priority(get_hostapi_name(sd.query_devices(i)))
)

print("Candidate input endpoints:")
for idx in STATE.candidate_indices:
    dev = sd.query_devices(idx)
    api = get_hostapi_name(dev) or "unknown"
    print(f"  {idx}: {dev['name']} [{api}] in={dev['max_input_channels']}")


# -------------------------------
# Start Audio Stream
# -------------------------------
STATE.stream = None
try:
    start_audio_stream(STATE, audio_callback)
except Exception as exc:
    QtWidgets.QMessageBox.critical(
        main_win,
        "Stream Failed",
        f"Unable to start audio stream:\n{exc}",
    )


# -------------------------------
# Qt Timer to update waterfall
# -------------------------------
timer = QtCore.QTimer()
timer.timeout.connect(update_waterfall)
timer.start(30)

# Open settings once after app starts; applying here restarts stream with selected FFT.
QtCore.QTimer.singleShot(0, open_processing_settings_dialog)
print("Audio waterfall running...")

QtWidgets.QApplication.instance().exec_()

# pyqt should be done, so save out the dataframe and clean up details
# originally tried the app.aboutToQuit signal, but it seems to be unreliable in some environments and can be triggered multiple times, causing issues with the audio stream and file saving. Instead, we call the cleanup function directly after the event loop exits to ensure it runs exactly once.
clean_up_and_save_data()
print("Application exited.")
print("Cleanup complete.")