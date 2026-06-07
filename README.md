# ADS-B Aircraft Receiver Dashboard

Public-safe dashboard and documentation for an SDR-based ADS-B aircraft receiver workflow.

## Project period

Representative of work from my RF / aviation portfolio, aligned with private pilot training and SDR receiver projects. This repository was created later as a public-safe evidence artifact because the original local files and RF captures are not suitable for public release.

## What this demonstrates

- 1090 MHz ADS-B / Mode S receiver signal chain
- RTL-SDR-style RF front end and aircraft telemetry workflow
- Preamble detection, frame decoding, ICAO/callsign/altitude/range logging
- Dashboard-style visualization for aircraft tracks and signal quality
- Hardware awareness for antennas, filters, LNAs, coax, and Linux logging setup

## Hardware to run the real project

- RTL-SDR Blog V4, Airspy, or comparable SDR receiver
- 1090 MHz ADS-B antenna
- Optional 1090 MHz band-pass filter and LNA
- SMA adapters, USB extension, and low-loss coax
- Laptop or Raspberry Pi running Linux
- `dump1090`, `readsb`, or similar ADS-B decoder
- Python for logging, plotting, and dashboard integration

## Resume-safe description

Built an SDR-based aircraft receiver workflow for real-time ADS-B decoding and visualization. Implemented RF signal processing, Mode S telemetry parsing, aircraft logging, and a public-safe dashboard showing signal quality, decoded traffic, and hardware stack.

