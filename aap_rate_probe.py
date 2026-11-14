#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Measure the AirPods head-sensor rate: same connection code as the controller, nothing else running.

    python aap_rate_probe.py                       # 10 s at 10000 µs (the controller's default, ~100 Hz)
    python aap_rate_probe.py --sweep 20000,10000,5000,2500
    python aap_rate_probe.py --address AA:BB:CC:DD:EE:FF --seconds 20

Runs on Linux (BlueZ socket) and on Windows (windows/aap-poc driver), so the two can be compared line
for line. The rate is counted in samples, not packets: one Bluetooth packet often carries several.
"""
import argparse
import logging
import select
import statistics
import struct
import sys
import time

import airpods_mouse_controller as m


def summarize(samples, packets, per_packet, max_len):
    """samples: [(host time s, device timestamp ns)] in arrival order -> dict of figures."""
    out = {"packets": packets, "samples": len(samples), "max_packet_bytes": max_len,
           "samples_per_packet": dict(sorted(per_packet.items()))}
    if len(samples) < 3:
        return out
    host = samples[-1][0] - samples[0][0]
    dev = (samples[-1][1] - samples[0][1]) / 1e9
    dts = [(b[1] - a[1]) / 1e9 for a, b in zip(samples, samples[1:])]
    med = statistics.median(dts)
    out.update({
        "hz_host_clock": (len(samples) - 1) / host if host > 0 else 0.0,
        "hz_device_clock": (len(samples) - 1) / dev if dev > 0 else 0.0,
        "median_dt_ms": med * 1e3,
        "out_of_order": sum(1 for d in dts if d <= 0),            # must be 0: packets reordered on the way
        "gaps": sum(1 for d in dts if med > 0 and d > 2.5 * med),  # samples lost or late
        "longest_gap_ms": max(dts) * 1e3,
    })
    return out


def measure(aap, seconds, out=print, settle=1.0):
    """Count the samples of `seconds` of streaming, starting at the first one (30 s to get there)."""
    aap.start_tracking()
    samples, per_packet, packets, max_len = [], {}, 0, 0
    t_wait = t_start = time.perf_counter()
    t_first = None
    while True:
        now = time.perf_counter()
        if t_first is None and now - t_wait > 30:
            out("  no sample in 30 s: earbud in the ear / out of the case?")
            break
        if t_first is not None and now - t_first >= seconds:
            break
        if t_first is None and now - t_start > 4:        # like the controller: ask again
            aap.start_tracking()
            t_start = now
        r, _, _ = select.select([aap.sock], [], [], 0.2)
        if not r:
            continue
        pkt = aap.sock.recv(8192)
        if not pkt:
            raise OSError("channel closed")
        now = time.perf_counter()
        blocks = aap.process(pkt)
        if not blocks or now - t_wait < settle:            # leftovers of the previous interval
            continue
        if t_first is None:
            t_first = now
        packets += 1
        max_len = max(max_len, len(pkt))
        per_packet[len(blocks)] = per_packet.get(len(blocks), 0) + 1
        samples.extend((now, struct.unpack_from("<Q", b, 1)[0]) for b in blocks)
    return summarize(samples, packets, per_packet, max_len)


def report(interval_us, s, out=print):
    out(f"interval {interval_us} µs (asked {1e6 / interval_us:.0f} Hz)")
    if "hz_host_clock" not in s:
        out(f"  only {s['samples']} samples in {s['packets']} packets: no rate")
        return
    out(f"  rate            {s['hz_host_clock']:.1f} Hz by this computer's clock, "
        f"{s['hz_device_clock']:.1f} Hz by the AirPods' timestamps")
    out(f"  samples         {s['samples']} in {s['packets']} packets, per packet {s['samples_per_packet']}")
    out(f"  median interval {s['median_dt_ms']:.2f} ms, longest {s['longest_gap_ms']:.1f} ms, "
        f"gaps {s['gaps']}, out of order {s['out_of_order']}")
    out(f"  largest packet  {s['max_packet_bytes']} bytes")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", default="auto")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--interval-us", type=int, default=10000)
    ap.add_argument("--sweep", help="comma-separated intervals in µs, e.g. 20000,10000,5000,2500")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    intervals = [int(x) for x in args.sweep.split(",")] if args.sweep else [args.interval_us]
    if m.WINDOWS:
        import aap_windows
        aap_windows.raise_timer_resolution()
    aap = m.AAPClient(args.address, True, intervals[0])
    try:
        aap.connect()
    except OSError as e:
        print(f"Cannot connect: {e}", file=sys.stderr)
        return 1
    print(f"Connected to {aap.addr}, model 0x{aap.pid} on {sys.platform}")
    ok = False
    try:
        for us in intervals:
            aap.interval_us = us
            s = measure(aap, args.seconds)
            report(us, s)
            ok = ok or s["samples"] > 0
        proto = "RemoteHID" if aap.rhid_motion is not None else "SensorDataWX"
        print(f"protocol {proto}; RemoteHID devices announced: {sorted(aap.rhid_devs) or 'none'}; "
              f"position: {aap.place or 'unknown'}")
        if aap.pid == "2013" and "devmotion6" not in aap.rhid_devs:
            print("AirPods 3 did not announce devmotion6: the receive MTU is probably below 2582.")
    except (OSError, KeyboardInterrupt) as e:
        print(f"Stopped: {e!r}", file=sys.stderr)
    finally:
        aap.close()
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
