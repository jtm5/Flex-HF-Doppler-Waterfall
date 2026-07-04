import sounddevice as sd


def find_target_input_devices(devices, preferred_device_index, target_audio_out_name):
    candidates = []

    if preferred_device_index is not None and 0 <= preferred_device_index < len(devices):
        preferred = devices[preferred_device_index]
        print("Have to use the preferred device")
        if preferred["max_input_channels"] > 0:
            print(
                f"Using configured device index {preferred_device_index}: {preferred['name']}"
            )
            candidates.append(preferred_device_index)
        else:
            print(
                f"Configured index {preferred_device_index} is not input-capable: {preferred['name']}"
            )

    match_tokens = ["dax", "1"] # when SmartSDR v4 came out, they dropped the audio part of name["audio", "1"]
    for idx, dev in enumerate(devices):
        if dev["max_input_channels"] <= 0:
            continue

        name = dev["name"].lower()
        direct_match = target_audio_out_name.lower() in name
        token_match = all(token in name for token in match_tokens)
        rx_rs_match = ("rx 1" in name or "rs 1" in name)
        if direct_match or (token_match and rx_rs_match):
            if idx not in candidates:
                candidates.append(idx)

    return candidates


def get_hostapi_name(device_info):
    try:
        hostapi = sd.query_hostapis(device_info["hostapi"])
        return str(hostapi.get("name", ""))
    except Exception:
        return ""


def hostapi_priority(name):
    value = name.lower()
    if "wasapi" in value:
        return 0
    if "wdm-ks" in value:
        return 1
    if "directsound" in value:
        return 2
    if "mme" in value:
        return 3
    return 4


def start_audio_stream(state, audio_callback):
    if state.stream is not None:
        try:
            state.stream.stop()
            state.stream.close()
        except Exception:
            pass
        state.stream = None

    errors = []
    for idx in state.candidate_indices:
        dev = sd.query_devices(idx)
        in_channels = max(1, min(2, int(dev["max_input_channels"])))
        api = get_hostapi_name(dev) or "unknown"

        attempts = [
            {
                "label": f"device {idx} [{api}] blocksize={state.input_samples_per_fft}",
                "device": idx,
                "channels": in_channels,
                "blocksize": state.input_samples_per_fft,
            },
            {
                "label": f"device {idx} [{api}] blocksize=0(auto)",
                "device": idx,
                "channels": in_channels,
                "blocksize": 0,
            },
        ]

        for attempt in attempts:
            try:
                print(
                    f"Trying stream mode: {attempt['label']} "
                    f"at {state.sample_rate} Hz"
                )
                state.stream = sd.InputStream(
                    device=attempt["device"],
                    samplerate=state.sample_rate,
                    channels=attempt["channels"],
                    dtype="float32",
                    blocksize=attempt["blocksize"],
                    callback=audio_callback,
                )
                state.stream.start()
                print("Stream started OK")
                return
            except Exception as exc:
                errors.append(f"{attempt['label']}: {exc}")
                state.stream = None

    detail = " | ".join(errors)
    raise RuntimeError(f"Stream failed after fallback attempts: {detail}")
