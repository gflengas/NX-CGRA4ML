"""NG-ULTRA DevKit rail power over I2C.

The DevKit measures its supply rails with an ADS1115 ADC behind two ADG728
multiplexers, one for the rail voltages and one for the current-sense
amplifier outputs. The host reaches that I2C bus through the ANGIE probe's
second USB interface, beside OpenOCD's JTAG, with NanoXplore's I2cLib
(nx_embedded_tools). Only the ADC, the multiplexers and the sensors are
accessed; the I/O expanders that drive resets and boot mode are never touched.

    python -m ngultra.power --out samples.csv [--seconds N]

samples one row per sweep of the rails (host time, then V, A and W per rail,
their sum and the 12 V input) until stopped or N seconds pass. `summarize`
averages the rows inside time windows.
"""

import argparse
import csv
import math
import signal
import sys
import time
from pathlib import Path

from .board import nxtools

# Rail: (voltage mux channel, (current mux channel, sense resistor ohm,
# amplifier gain)), from NanoXplore's DevKit table (ul_dv/ul_dv.py).
RAILS = {
    "VDD_CORE": (4, (3, 0.005, 6.2)),      # FPGA fabric core, 1.0 V
    "SOC_CORE": (3, (1, 0.010, 6.2)),      # ARM SoC core, 1.0 V
    "1V8D": (6, (2, 0.010, 13)),
    "3V3D": (5, (0, 0.007, 10)),
    "HSSL_VDD1V8A": (0, (5, 0.05, 100)),   # high-speed serial links (unused)
    "HSSL_TXVDDA": (1, (6, 0.05, 100)),
    "HSSL_VDDCORE": (2, (7, 0.005, 100)),
}
INPUT_CURRENT = (4, 0.007, 50)             # 12 V board input current
INPUT_VOLTS = 12.0                         # nominal: the input voltage is not measured
MUX_ADDRS = (0x4C, 0x4E)                   # ADG728: voltages, currents
ADC_ADDR = 0x48                            # ADS1115
ADC_CONFIG, ADC_CONVERSION = 1, 0
ADC_FULL_SCALE = 4.096                     # PGA +-4.096 V
ADC_RATE_128SPS = 4
CONVERSION_TIMEOUT_S = 1.0


def columns():
    """Returns the CSV header of a sample row."""
    names = ["time"]
    for rail in RAILS:
        names += [f"{rail}_V", f"{rail}_A", f"{rail}_W"]
    return names + ["RAILS_W", "INPUT_A", "INPUT_W"]


class Monitor:
    """The DevKit's rail monitor on the ANGIE I2C interface."""

    def __init__(self):
        sys.path.insert(0, str(nxtools() / "py"))
        from libnxembeddedtools.i2c.i2c_lib import I2cLib
        self.i2c = I2cLib()

    def _input_volts(self, mux_channel, adc_input):
        """Converts one multiplexer channel on ADC input 0 (voltage) or 1
        (current); returns the volts at the ADC input."""
        self.i2c.write([MUX_ADDRS[adc_input], 1 << mux_channel])
        # Single shot, single-ended AIN<adc_input>, PGA +-4.096 V, 128 SPS.
        high = (1 << 7) | (1 << 6) | (adc_input << 4) | (1 << 1) | 1
        self.i2c.write([ADC_ADDR, ADC_CONFIG, high, ADC_RATE_128SPS << 5])
        deadline = time.monotonic() + CONVERSION_TIMEOUT_S
        while not self.i2c.read([2, ADC_ADDR, ADC_CONFIG])[0] >> 7:
            if time.monotonic() > deadline:
                raise RuntimeError("ADS1115 conversion did not finish")
        high_byte, low_byte = self.i2c.read([2, ADC_ADDR, ADC_CONVERSION])[:2]
        value = (high_byte << 8) | low_byte
        if value >= 0x8000:
            value -= 0x10000
        return ADC_FULL_SCALE * value / 32767

    def sample(self):
        """Returns one row: host time, V/A/W per rail, input A/W."""
        row = [time.time()]
        total = 0.0
        for volts_channel, (current_channel, ohm, gain) in RAILS.values():
            volts = self._input_volts(volts_channel, 0)
            amps = self._input_volts(current_channel, 1) / (ohm * gain)
            row += [volts, amps, volts * amps]
            total += volts * amps
        channel, ohm, gain = INPUT_CURRENT
        amps = self._input_volts(channel, 1) / (ohm * gain)
        return row + [total, amps, INPUT_VOLTS * amps]


def summarize(csv_path, windows, settle_s=1.0):
    """Averages the samples inside each named group of (start, end) host-time
    windows, skipping the first settle_s seconds of every window.

    Returns:
        {name: {"samples": n, column: {"mean": x, "std": s}}} for every
        column but time.
    """
    with open(csv_path, newline="") as stream:
        rows = [{k: float(v) for k, v in row.items()} for row in csv.DictReader(stream)]
    result = {}
    for name, spans in windows.items():
        inside = [r for r in rows
                  if any(start + settle_s <= r["time"] <= end for start, end in spans)]
        stats = {"samples": len(inside)}
        for column in columns()[1:]:
            values = [r[column] for r in inside]
            if values:
                mean = sum(values) / len(values)
                std = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
                stats[column] = {"mean": mean, "std": std}
        result[name] = stats
    return result


def main(argv=None):
    """`python -m ngultra.power`: see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m ngultra.power", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True, help="new CSV file")
    parser.add_argument("--seconds", type=float, help="stop after this long (default: until killed)")
    args = parser.parse_args(argv)
    monitor = Monitor()
    end = None if args.seconds is None else time.monotonic() + args.seconds
    # Stop between sweeps only: an I2C transfer cut short leaves a stale reply
    # in the probe that breaks its next user (NanoXplore's tools included).
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
    signal.signal(signal.SIGINT, lambda *_: stop.append(True))
    with args.out.open("x", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns())
        while not stop and (end is None or time.monotonic() < end):
            writer.writerow([f"{v:.6f}" for v in monitor.sample()])
            stream.flush()


if __name__ == "__main__":
    main()
