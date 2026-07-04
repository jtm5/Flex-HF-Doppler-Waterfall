from dataclasses import dataclass, field
from collections import deque
from queue import Empty, Queue
import os
import numpy as np

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_FFT_SIZE = 65536
MIN_FFT_SIZE = 256
MAX_FFT_SIZE = 262144
WATERFALL_HEIGHT = 400
MIN_DB_DEFAULT = -46
MAX_DB_DEFAULT = 10
MAX_ZOOM_FACTOR = 100
DEFAULT_DECIMATION_FACTOR = 12
DEFAULT_LPF_TAP_COUNT = 127
LABEL_PIXEL_SPACING = 95
INPUT_CHANNEL_INDEX = 0
TARGET_AUDIO_OUT_NAME = "DAX RX 1 (FlexRadio Systems"
PREFERRED_DEVICE_INDEX = 11
SAMPLE_ATTENUATION = 0.1
CSV_FILENAME = "peak_frequencies.csv"
CSV_DIRECTORY = "D:\\Data\\Ham Radio\\HAMSci Local Experiments\\HF DOPPLER ANALYSIS"  # replaced os.getcwd() as default
MAG_EPSILON = 1e-12


@dataclass
class AppState:
    sample_rate: int = DEFAULT_SAMPLE_RATE
    fft_size: int = DEFAULT_FFT_SIZE
    decimation_factor: int = DEFAULT_DECIMATION_FACTOR
    lpf_tap_count: int = DEFAULT_LPF_TAP_COUNT
    sample_attenuation: float = SAMPLE_ATTENUATION
    csv_filename: str = CSV_FILENAME
    results_directory: str = CSV_DIRECTORY
    tx_station: str = "CHU7"
    radio_frequency_khz: float = 0.0
    rx_station: str = ""
    radio_host: str = "10.0.0.252"
    radio_port: int = 4992
    radio_send: object = None
    radio_stop: object = None
    effective_sample_rate: float = 0.0
    nyquist_hz: float = 0.0
    min_span_hz: float = 1.0
    fft_bins: int = 0
    lpf_cutoff_hz: float = 0.0
    window: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    input_samples_per_fft: int = 0
    decim_lpf_taps: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    lpf_state: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    decimated_buffer: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    waterfall: np.ndarray = field(default_factory=lambda: np.zeros((WATERFALL_HEIGHT, 1), dtype=np.float32))
    waterfall_row_timestamps: object = field(
        default_factory=lambda: deque(["--:--:--"] * WATERFALL_HEIGHT, maxlen=WATERFALL_HEIGHT)
    )
    waterfall_queue: Queue = field(default_factory=Queue)
    waterfall_lines_saved: int = 0
    waterfall_image_save_count: int = 0
    stream: object = None
    candidate_indices: list = field(default_factory=list)


def design_lowpass_fir(sample_rate_hz, cutoff_hz, tap_count):
    cutoff_hz = min(cutoff_hz, (sample_rate_hz / 2) * 0.999)
    fc = cutoff_hz / sample_rate_hz
    n = np.arange(tap_count) - (tap_count - 1) / 2
    taps = 2.0 * fc * np.sinc(2.0 * fc * n)
    taps *= np.hamming(tap_count)
    taps /= np.sum(taps)
    return taps.astype(np.float32)


def valid_fft_sizes(min_size=MIN_FFT_SIZE, max_size=MAX_FFT_SIZE):
    size = 1
    while size < min_size:
        size <<= 1

    sizes = []
    while size <= max_size:
        sizes.append(size)
        size <<= 1
    return sizes


def is_power_of_two(value):
    value = int(value)
    return value > 0 and (value & (value - 1)) == 0


def clear_audio_queue(state):
    while True:
        try:
            state.waterfall_queue.get_nowait()
        except Empty:
            break


def apply_runtime_options(state, values):
    state.sample_attenuation = values["attenuation"]
    state.csv_filename = values["csv_filename"]
    state.results_directory = values["csv_directory"]
    state.tx_station = values.get("tx_station", state.tx_station)
    state.radio_frequency_khz = values.get("radio_frequency_khz", state.radio_frequency_khz)
    state.rx_station = values.get("rx_station", state.rx_station)
    state.radio_host = values.get("radio_host", state.radio_host)
    state.radio_port = int(values.get("radio_port", state.radio_port))
    os.makedirs(state.results_directory, exist_ok=True)


def append_peak_record(state, timestamp, peak_freq_hz, peak_mag_db):
    csv_path = os.path.join(state.results_directory, state.csv_filename)
    has_header = os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
    with open(csv_path, "a", encoding="utf-8") as file_obj:
        if not has_header:
            file_obj.write("timestamp,peak_freq_hz,peak_mag_db\n")
        file_obj.write(f"{timestamp},{peak_freq_hz:.4f},{peak_mag_db:.2f}\n")


def apply_processing_config(
    state,
    sample_rate,
    fft_size,
    decimation_factor,
    lpf_tap_count=None,
    lpf_cutoff_hz=None,
    max_zoom_factor=MAX_ZOOM_FACTOR,
    waterfall_height=WATERFALL_HEIGHT,
):
    if sample_rate < 1:
        raise ValueError("SAMPLE_RATE must be >= 1")

    fft_size = int(fft_size)
    if fft_size < MIN_FFT_SIZE or fft_size > MAX_FFT_SIZE:
        raise ValueError(f"FFT_SIZE must be between {MIN_FFT_SIZE} and {MAX_FFT_SIZE}")
    if not is_power_of_two(fft_size):
        raise ValueError("FFT_SIZE must be a power of two")
    if decimation_factor < 1:
        raise ValueError("DECIMATION_FACTOR must be >= 1")

    if lpf_tap_count is None:
        lpf_tap_count = state.lpf_tap_count
    if int(lpf_tap_count) < 3:
        raise ValueError("LPF_TAP_COUNT must be >= 3")

    state.sample_rate = int(sample_rate)
    state.fft_size = fft_size
    state.decimation_factor = int(decimation_factor)
    state.lpf_tap_count = int(lpf_tap_count)
    if state.lpf_tap_count % 2 == 0:
        state.lpf_tap_count += 1

    state.effective_sample_rate = state.sample_rate / state.decimation_factor
    state.nyquist_hz = state.effective_sample_rate / 2
    state.min_span_hz = max(1.0, state.nyquist_hz / max_zoom_factor)
    state.fft_bins = state.fft_size // 2 + 1

    default_cutoff = 0.45 * state.effective_sample_rate
    if lpf_cutoff_hz is None:
        state.lpf_cutoff_hz = default_cutoff
    else:
        state.lpf_cutoff_hz = float(lpf_cutoff_hz)
    state.lpf_cutoff_hz = float(np.clip(state.lpf_cutoff_hz, 10.0, state.sample_rate * 0.499))

    state.window = np.hanning(state.fft_size).astype(np.float32)
    state.input_samples_per_fft = state.fft_size * state.decimation_factor
    state.decim_lpf_taps = design_lowpass_fir(state.sample_rate, state.lpf_cutoff_hz, state.lpf_tap_count)
    state.lpf_state = np.zeros(state.lpf_tap_count - 1, dtype=np.float32)
    state.decimated_buffer = np.zeros(0, dtype=np.float32)
    state.waterfall = np.zeros((waterfall_height, state.fft_bins), dtype=np.float32)
    state.waterfall_row_timestamps = deque(["--:--:--"] * waterfall_height, maxlen=waterfall_height)

    clear_audio_queue(state)
