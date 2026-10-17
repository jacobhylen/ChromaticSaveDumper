"""
Dump the battery save (SRAM) of an original Game Boy / Game Boy Color cartridge
using a ModRetro Chromatic over USB.

How it works (reverse-engineered from ModRetro's MR Updater / Cart Clinic):
  1. The Cart Clinic FPGA bitstream is loaded into the FPGA's *volatile* SRAM
     with openFPGALoader (cable "gwu2x"). Nothing is written to flash.
  2. The bitstream exposes a raw cartridge bus over the Chromatic's USB serial
     port (VID 0x374E, PID 0x0101, 115200 baud). Commands and replies are
     4 bytes: struct '<BHB' = (command id, 16-bit address, data).
  3. Save RAM is read with standard MBC register writes (RAM enable, RAM bank
     select). The script never writes to the save area 0xA000-0xBFFF.
  4. The save is read twice and only written to disk if both reads match.
  5. The FPGA is reset, which reloads the normal Chromatic firmware from flash.

Usage:
  python dump_save.py                 # writes "<GAME TITLE>.sav" next to the script
  python dump_save.py -o pokemon.sav
  python dump_save.py --header-only   # just identify the cart, no save read
"""

import argparse
import struct
import subprocess
import sys
import time
from pathlib import Path

import serial
from serial.tools import list_ports

HERE = Path(__file__).resolve().parent
DEFAULT_LOADER = HERE / "bin" / "openFPGALoader.exe"
DEFAULT_BITSTREAM = HERE / "bin" / "cart_clinic_260820.fs"
FPGA_CABLE = "gwu2x"
CHROMATIC_VID, CHROMATIC_PID = 0x374E, 0x0101
BAUD = 115200

CMD_LOOPBACK = 0x01
CMD_READ_CART_BYTE = 0x02
CMD_WRITE_CART_BYTE = 0x03
PACKET = struct.Struct("<BHB")  # id, addr, data -- used for commands and replies

BLOCK = 256  # bytes requested per pipelined batch (same as Cart Clinic)
RETRIES = 5

# Cartridge type byte (0x0147) -> (mapper, has battery-backed RAM)
CART_TYPES = {
    0x00: ("ROM", False), 0x01: ("MBC1", False), 0x02: ("MBC1", False), 0x03: ("MBC1", True),
    0x05: ("MBC2", False), 0x06: ("MBC2", True),
    0x08: ("ROM", False), 0x09: ("ROM", True),
    0x0F: ("MBC3", True), 0x10: ("MBC3", True), 0x11: ("MBC3", False), 0x12: ("MBC3", False), 0x13: ("MBC3", True),
    0x19: ("MBC5", False), 0x1A: ("MBC5", False), 0x1B: ("MBC5", True),
    0x1C: ("MBC5", False), 0x1D: ("MBC5", False), 0x1E: ("MBC5", True),
    0x20: ("MBC6", True), 0x22: ("MBC7", True),
    0xFC: ("CAMERA", True), 0xFD: ("TAMA5", True), 0xFE: ("HuC3", True), 0xFF: ("HuC1", True),
}
SUPPORTED_MAPPERS = {"ROM", "MBC1", "MBC2", "MBC3", "MBC5", "CAMERA", "HuC1"}

# RAM size byte (0x0149) -> total bytes
RAM_SIZES = {0x00: 0, 0x01: 2 * 1024, 0x02: 8 * 1024, 0x03: 32 * 1024, 0x04: 128 * 1024, 0x05: 64 * 1024}


class CartBus:
    def __init__(self, port: serial.Serial):
        self.port = port

    def _exchange(self, packets: list[bytes]) -> list[tuple[int, int, int]]:
        """Send a batch of commands and collect the same number of 4-byte replies."""
        self.port.reset_input_buffer()
        self.port.write(b"".join(packets))
        self.port.flush()
        want = len(packets) * PACKET.size
        buf = bytearray()
        deadline = time.monotonic() + 2.0 + len(packets) * 0.002
        while len(buf) < want and time.monotonic() < deadline:
            chunk = self.port.read(want - len(buf))
            if chunk:
                buf.extend(chunk)
        if len(buf) < want:
            raise TimeoutError(f"got {len(buf)} of {want} reply bytes")
        return [PACKET.unpack_from(buf, i) for i in range(0, want, PACKET.size)]

    def loopback(self) -> bool:
        payload = bytes([0x12, 0x34, 0x56])
        reply = self._exchange([bytes([CMD_LOOPBACK]) + payload])[0]
        return PACKET.pack(*reply) == bytes([CMD_LOOPBACK]) + payload

    def write_reg(self, addr: int, value: int) -> None:
        """Write an MBC control register. Refuses anything in the save RAM window."""
        if not (0x0000 <= addr < 0x8000):
            raise ValueError(f"refusing to write outside MBC register space: 0x{addr:04X}")
        for _ in range(RETRIES):
            try:
                cmd_id, raddr, rdata = self._exchange([PACKET.pack(CMD_WRITE_CART_BYTE, addr, value)])[0]
                if cmd_id == CMD_WRITE_CART_BYTE and raddr == addr and rdata == value:
                    return
            except TimeoutError:
                pass
        raise IOError(f"register write 0x{addr:04X}=0x{value:02X} not acknowledged")

    def read(self, addr: int, length: int) -> bytes:
        out = bytearray()
        for start in range(addr, addr + length, BLOCK):
            n = min(BLOCK, addr + length - start)
            out.extend(self._read_block(start, n))
        return bytes(out)

    def _read_block(self, start: int, n: int) -> bytes:
        packets = [PACKET.pack(CMD_READ_CART_BYTE, start + i, 0) for i in range(n)]
        for attempt in range(RETRIES):
            try:
                replies = self._exchange(packets)
            except TimeoutError:
                continue
            data = {}
            for cmd_id, raddr, rdata in replies:
                if cmd_id == CMD_READ_CART_BYTE:
                    data[raddr] = rdata
            if all(start + i in data for i in range(n)):
                return bytes(data[start + i] for i in range(n))
        raise IOError(f"failed to read block at 0x{start:04X} after {RETRIES} attempts")


def parse_header(h: bytes) -> dict:
    """h is the 0x0100-0x014F header region."""
    # 0x0143 is the CGB flag; GBC-era carts also use 0x013F-0x0142 for a manufacturer code
    raw_title = h[0x34:0x3F] if h[0x43] in (0x80, 0xC0) else h[0x34:0x43]
    title = "".join(chr(b) for b in raw_title.split(b"\0")[0] if 0x20 <= b < 0x7F).strip()
    checksum = 0
    for b in h[0x34:0x4D]:
        checksum = (checksum - b - 1) & 0xFF
    cart_type = h[0x47]
    mapper, battery = CART_TYPES.get(cart_type, (f"unknown(0x{cart_type:02X})", False))
    ram = 512 if mapper == "MBC2" else RAM_SIZES.get(h[0x49], 0)
    return {
        "title": title or "UNTITLED",
        "cart_type": cart_type,
        "mapper": mapper,
        "battery": battery,
        "ram_size": ram,
        "checksum_ok": checksum == h[0x4D],
        "cgb": h[0x43] in (0x80, 0xC0),
    }


def read_save(bus: CartBus, info: dict) -> bytes:
    mapper, size = info["mapper"], info["ram_size"]
    bank_size = min(size, 0x2000)
    banks = max(1, size // 0x2000)
    out = bytearray()
    bus.write_reg(0x0000, 0x0A)  # RAM enable
    try:
        if mapper == "MBC1":
            bus.write_reg(0x6000, 0x01)  # RAM banking mode
        for bank in range(banks):
            if mapper != "ROM" and mapper != "MBC2":
                bus.write_reg(0x4000, bank)
            data = bus.read(0xA000, bank_size)
            if mapper == "MBC2":
                data = bytes((b & 0x0F) | 0xF0 for b in data)  # 4-bit RAM
            out.extend(data)
            print(f"  bank {bank + 1}/{banks} read", flush=True)
    finally:
        if mapper not in ("ROM", "MBC2"):
            bus.write_reg(0x4000, 0x00)
        if mapper == "MBC1":
            bus.write_reg(0x6000, 0x00)
        bus.write_reg(0x0000, 0x00)  # RAM disable -- protects the save on power loss
    return bytes(out)


def find_chromatic_port(timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for p in list_ports.comports():
            if p.vid == CHROMATIC_VID and p.pid == CHROMATIC_PID:
                return p.device
        time.sleep(0.25)
    raise RuntimeError("Chromatic serial port (374E:0101) not found")


def open_bus(timeout_s: float = 15.0) -> tuple[serial.Serial, CartBus]:
    deadline = time.monotonic() + timeout_s
    last_err = None
    while time.monotonic() < deadline:
        try:
            port = serial.Serial(find_chromatic_port(deadline - time.monotonic()), BAUD, timeout=0.05)
            bus = CartBus(port)
            for _ in range(3):
                try:
                    if bus.loopback():
                        return port, bus
                except TimeoutError as e:
                    last_err = e
            port.close()
        except (serial.SerialException, RuntimeError) as e:
            last_err = e
        time.sleep(0.5)
    raise RuntimeError(f"Cart Clinic bridge did not respond to loopback: {last_err}")


def run_loader(loader: Path, *args: str, timeout: int = 60) -> None:
    cmd = [str(loader), "--cable", FPGA_CABLE, *args]
    print("  $", " ".join(cmd[1:]), flush=True)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"openFPGALoader failed ({res.returncode}):\n{res.stdout}\n{res.stderr}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", type=Path, help="output .sav path")
    ap.add_argument("--header-only", action="store_true", help="identify the cart without reading the save")
    ap.add_argument("--loader", type=Path, default=DEFAULT_LOADER)
    ap.add_argument("--bitstream", type=Path, default=DEFAULT_BITSTREAM)
    args = ap.parse_args()

    for f in (args.loader, args.bitstream):
        if not f.exists():
            print(f"Missing file: {f}", file=sys.stderr)
            return 2

    print("Switching Chromatic to Cart Clinic mode (volatile FPGA SRAM load)...")
    run_loader(args.loader, "--write-sram", "--skip-reset", str(args.bitstream))
    port = None
    try:
        time.sleep(1.0)  # USB bridge lives in the FPGA and re-enumerates
        port, bus = open_bus()
        print(f"Connected on {port.port}")

        header = bus.read(0x0100, 0x50)
        info = parse_header(header)
        print(f"Title:    {info['title']}  ({'GBC' if info['cgb'] else 'GB'})")
        print(f"Mapper:   {info['mapper']} (type 0x{info['cart_type']:02X}), battery: {info['battery']}")
        print(f"Save RAM: {info['ram_size']} bytes")
        if not info["checksum_ok"]:
            print("Header checksum mismatch: cart not read correctly. Reseat the cartridge and try again.")
            return 1
        if args.header_only:
            return 0
        if not info["battery"] or info["ram_size"] == 0:
            print("This cartridge has no battery-backed save RAM. Nothing to dump.")
            return 1
        if info["mapper"] not in SUPPORTED_MAPPERS:
            print(f"Mapper {info['mapper']} is not supported by this script.")
            return 1

        print("Reading save (pass 1)...")
        first = read_save(bus, info)
        print("Reading save (pass 2, verify)...")
        second = read_save(bus, info)
        if first != second:
            diff = sum(a != b for a, b in zip(first, second))
            print(f"Verification failed: {diff} bytes differ between reads. Nothing written.")
            return 1

        out = args.output or HERE / (
            "".join(c if c.isalnum() or c in " -_" else "_" for c in info["title"]).strip() + ".sav")
        if out.exists():
            out = out.with_name(f"{out.stem}_{time.strftime('%Y%m%d-%H%M%S')}{out.suffix}")
        out.write_bytes(first)
        print(f"Saved {len(first)} bytes to {out}")
        if info["mapper"] == "MBC3" and info["cart_type"] in (0x0F, 0x10):
            print("Note: this cart has a real-time clock; RTC state is not included in the .sav.")
        return 0
    finally:
        if port is not None:
            port.close()
        print("Resetting Chromatic back to normal firmware...")
        try:
            run_loader(args.loader, "--reset", timeout=30)
        except Exception as e:  # never leave the user without knowing how to recover
            print(f"Reset failed ({e}). Power-cycle the Chromatic to return to normal.")


if __name__ == "__main__":
    sys.exit(main())
