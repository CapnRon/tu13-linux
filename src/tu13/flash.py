"""Command-line firmware flasher for the ATORCH TU13 (C13P bootloader)."""
from __future__ import annotations

import argparse
import os
import sys
import time

from .protocol import TU13, TU13Error, device_info, find_device, in_bootloader


def _progress(done, total):
    width = 30
    filled = int(width * done / total)
    pct = done * 100 // total
    sys.stderr.write(
        f"\r[{'#' * filled}{'.' * (width - filled)}] {pct:3d}%  {done}/{total} packets"
    )
    sys.stderr.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Flash firmware to an ATORCH TU13 via its C13P bootloader."
    )
    parser.add_argument("image", help="firmware .bin (vendor image, sent verbatim)")
    parser.add_argument(
        "--enter-iap",
        action="store_true",
        help="reboot the device into bootloader mode first (sends page 0x03)",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)

    if not os.path.exists(args.image):
        print(f"error: {args.image}: no such file", file=sys.stderr)
        return 2
    image_size = os.path.getsize(args.image)
    packets = (image_size + 57) // 58

    node = find_device()
    if node is None:
        print("error: TU13 not found (VID 0483 PID 5750)", file=sys.stderr)
        return 1

    if not in_bootloader():
        if not args.enter_iap:
            print(
                "error: device is running its application firmware.\n"
                "       use --enter-iap to reboot it into the bootloader first.",
                file=sys.stderr,
            )
            return 1
        print("entering bootloader mode...", file=sys.stderr)
        device = TU13(node)
        try:
            device.open()
            if not device.enter_bootloader():
                print("error: device did not re-enumerate as bootloader", file=sys.stderr)
                return 1
        finally:
            device.close()
        time.sleep(2)
        node = find_device()
        if node is None:
            print("error: bootloader device not found", file=sys.stderr)
            return 1

    info = device_info()
    if info:
        print(f"device: {info[0]} ({info[1]}) serial {info[2]}", file=sys.stderr)
    print(f"image:  {args.image} ({image_size} bytes, {packets} packets)", file=sys.stderr)

    if not args.yes:
        answer = input("Flash this image? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("aborted", file=sys.stderr)
            return 1

    device = TU13(node)
    try:
        device.open()
        device.flash(
            open(args.image, "rb").read(),
            progress_cb=_progress,
            log_cb=lambda msg: print(f"\n{msg}", file=sys.stderr),
        )
    except TU13Error as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    finally:
        device.close()

    print("\ndone - the device is flashing and will reboot into the application", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
