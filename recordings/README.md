# Light meter recordings

The CSV files in this folder contain illuminance readings received over Bluetooth from a UNI-T UT383BT light meter by this project's dashboard. Each file represents one recording started and stopped in the dashboard. The filename records the **start time in UTC**, using the form `light_YYYYMMDD_HHMMSS_microseconds_UTC.csv`. For example, `light_20261108_011321_191838_UTC.csv` was started at 01:13:21.191838 UTC on 8 November 2026.

## File format

Files are UTF-8 CSV with a header and one row per reading:

```csv
timestamp_utc,lux
2026-11-08T01:13:22.123456+00:00,250
```

- `timestamp_utc` is the UTC time the client received and parsed the Bluetooth notification, in ISO 8601 format. It is not a timestamp from the meter itself.
- `lux` is the illuminance value reported by the client, in lux (lx), as a nonnegative integer. Lux measures visible light weighted by human visual sensitivity; it is not irradiance (such as mW/cm²), radiant power, or a measurement of a specific wavelength. Lux alone cannot reliably determine red-light irradiance or dose.

The meter should be set to **LUX** when recording. The upstream parser does not reliably validate the unit in the payload, so a recording made with the meter in foot-candle (FC) mode could be incorrectly labelled as lux. The files do not independently verify the unit or meter calibration.

## Interpreting a recording

The dashboard writes readings received after **Start recording** and before **Stop recording**. It also finalizes an active recording on normal server shutdown. A `.csv.part` file is an in-progress or incomplete recording; it may remain after an interruption or write failure.

Readings are requested at a configurable interval, normally one second, but the interval can change during a recording. Use the timestamps for timing; do not assume evenly spaced or complete samples. Bluetooth interruptions, missed readings, or the meter being off can create gaps. The files do not mark disconnections, changes in request interval, measurement mode, overload, or device settings. A chart reset does not stop or clear the recording.

The CSV does not describe the light source, distance, orientation, ambient conditions, experiment, or location. Do not infer these from the filename or values; use separately supplied notes. The UT383BT measures illuminance at its sensor, and comparisons depend on the measurement setup.

For application setup and usage, see the [project README](../README.md).
