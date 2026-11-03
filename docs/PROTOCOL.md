# TU13 HID protocol

All findings below were obtained by static analysis of the vendor application
(`UsbTester.exe` 1.0.5, Qt5, statically linked hidapi) with Ghidra, and
confirmed against hardware (application firmware V1.1.5, bootloader
`C13P V1.0.2`).

## Transport

| Item | Value |
|---|---|
| USB IDs | `0483:5750` |
| Application product string | `ATORCH TU13 V1.1.5` (manufacturer `APP`) |
| Bootloader product string | `ATORCH C13P V1.0.2` (manufacturer `IAP`) |
| HID output report | ID `0x55`, 63 payload bytes |
| HID input report | ID `0xAA`, 63 payload bytes |
| Node | `/dev/hidrawN` (root by default - see `packaging/99-tu13.rules`) |

`hid_write` is called with a 64-byte buffer whose first byte is the report ID,
so the payload is 63 bytes.  This matters for CRC placement (below).

## Measurement poll

The vendor UI sends this roughly twice per second:

```
55 05 22 05 0b 00 8c ee ff      (9 bytes, padded with zeros to 63)
```

Reply:

```
AA 40 62 05 0b 00 eb 01 <9 x u32 LE> ...padding to 65 bytes? (64 total)
```

The nine little-endian `u32` fields start at report offsets `0x08`, `0x0C`,
`0x10`, ... `0x28`.  The vendor divides every field by **1,000,000**
(the float constant at `0x476098` is `1000000.0`), but the tiles need
per-field scaling:

| Offset | Meaning | Scale | Evidence |
|---|---|---|---|
| `0x08` | Voltage | /1e6 (V) | 5.10 V with a 5 V source; 2.69 V induced when floating |
| `0x0C` | Current | /1e6 (A) | 0.482 A under load |
| `0x10` | Power | /1e6 (W) | equals V x I |
| `0x14` | Energy | /1e6 (Wh) | accumulates while loaded |
| `0x18` | Capacity | /1e6 (Ah) | accumulates |
| `0x1C` | unknown | - | decays by halving: 4551 -> 2275 -> 1137 |
| `0x20` | unknown | - | same family as 0x1C |
| `0x24` | constant | - | frozen across reboots and a firmware reflash |
| `0x28` | constant | - | frozen across reboots and a firmware reflash |

Only `0x08`, `0x0C`, `0x10` and `0x14` change over seconds.  `0x1C`/`0x20`
change over minutes and are not line voltages (they do not track the CC
lines).  Temperature does **not** appear anywhere in this frame; the vendor
must fetch it with a command that has not been identified.

The reply's tail (offsets `0x2C`-`0x3F`) is constant across time, reboots and
a reflash - it looks like stored configuration rather than telemetry.

### Page commands

Nine-byte requests of the form:

```
55 05 09 <page> <param> 00 00 EE FF
```

Pages observed in the binary: `0x02`, `0x03`, `0x04`, `0x06`, `0x07`, `0x08`,
`0x09`, `0x0A`, `0x20`, `0x21`, `0x25`, `0x26`, `0x2D`, `0x2E`, `0x2F`,
`0x30`, `0x31`, `0x32`.

On tested firmware these commands are **not answered** - they appear to be
display/settings controls.  `0x03` makes the application reboot into the
bootloader.

> Beware: while another process is polling, a page command can appear to
> "reply" - the read simply catches the other process's pending poll reply.

## Bootloader protocol (C13P)

The bootloader answers nothing except its own session frames.

### Start a session

```
host -> device   55 | AA 06 <count LE16> <crc16>      (padded with zeros)
device -> host   AA | BB 06 <count LE16> <crc16>
```

* `count` = `ceil(image_size / 58)`
* the request CRC is CRC-16/MODBUS over `AA 06 <count LE16>` (4 bytes)
* the reply CRC is CRC-16/MODBUS over `BB 06 <count LE16>` (4 bytes),
  stored little-endian at payload offsets `len-2`, `len-1`

The device does not time out and does not abort: it waits indefinitely for
data packets.  A zero-count session is acknowledged and then also waits.

### Data packets

```
host -> device   55 | CC <len> <index LE16> <58 bytes> <crc low byte>
```

* `len` = `0x40` for full chunks, `remaining + 6` for the final chunk
* `index` starts at 0 and increments by one per packet
* CRC = CRC-16/MODBUS over `CC <len> <index LE16> <data>`
  (`len - 2` bytes), but only the **low byte** is transmitted: the vendor
  builds a 64-byte packet and the HID report carries 63 payload bytes, so the
  high byte falls off the end.  The vendor application works this way, so the
  bootloader must accept it.

Acknowledgement:

```
device -> host   AA | DD <len> <index LE16> ...
```

Wait for each acknowledgement before sending the next packet.  The final
packet may be acknowledged with `AA FF` instead of the data ack.

### Completion

```
device -> host   AA | EE ...    transfer complete, device is flashing
device -> host   AA | FF ...    flash complete, device reboots
```

The device erases and writes internally only after all packets are received
and acknowledged.  A failed transfer leaves the unit in bootloader mode,
where another attempt is possible.

### Firmware images

The vendor `.bin` files are obfuscated (no valid ARM vector table at the
start); the bootloader decrypts them.  Send the file byte-for-byte.

## Reference: Ghidra findings

| Address | Role |
|---|---|
| `0x434590` | `hid_write` wrapper |
| `0x402f80` | send with `0xAA` padding |
| `0x4330f0` | send 63-byte payload, report ID `0x55` |
| `0x42e3f0` | response parser (`AA 40 62 05` and `AA 05 40 ...` families) |
| `0x407ca0` | flash state machine (BB/DD/EE/FF handling) |
| `0x407c30` | handshake-ack CRC validator |
| `0x412760` | start-of-download slot (builds `AA 06`) |
| `0x4117d0` | page/poll sender used by the UI timer |
| `0x476098` | float constant `1000000.0` |
