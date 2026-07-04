import os
from pyqtgraph.Qt import QtWidgets, QtCore
import pyqtgraph as pg
import sounddevice as sd
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime
from queue import Empty, Queue


# saved dataframe filename
# D:\Data\Ham Radio\HAMSci Local Experiments\HF DOPPLER ANALYSIS\Good CHU7 Results\TID__CHU7 2jun26
plot_title = "CHU7_2026-06-01_212443_UTC_df_FreqMag"
saved_dataframe_path = "D:\\Data\\Ham Radio\\HAMSci Local Experiments\\HF DOPPLER ANALYSIS\\Good CHU7 Results\\TID__CHU7 2jun26"
# saved_dataframe_path = os.path.join(STATE.RESULTS_DIR, DATAFRAME_FILENAME)
df=pd.read_csv(saved_dataframe_path + "\\" + plot_title + ".csv")
df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")

print(df.size)

fig, ax1 = plt.subplots(figsize=(12, 6))
ax1.set_xlabel("Time")
ax1.set_ylabel("Peak Frequency (Hz)", color="tab:blue")
ax1.plot(df["timestamp"], df["peak_freq_hz"], color="tab:blue")
ax1.set_ylim(998.0,1002.0)

ax2 = ax1.twinx()
ax2.set_ylabel("Peak Magnitude (dB)", color="tab:red")
ax2.plot(df["timestamp"], df["peak_mag_db"], color="tab:red")

plt.title(plot_title)

plt.savefig(saved_dataframe_path + "\\" + plot_title + ".png")
plt.show()
# plt.close("all")