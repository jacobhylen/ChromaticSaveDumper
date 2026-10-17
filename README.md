# ChromaticSaveDumper

Back up the battery save of **original Game Boy / Game Boy Color cartridges** using a
[ModRetro Chromatic](https://modretro.com) connected over USB.

ModRetro's own Cart Clinic (part of MR Updater) only backs up saves from ModRetro carts.
That restriction lives in the desktop app, not the hardware: the Cart Clinic FPGA firmware
exposes a raw cartridge bus, so any cart can be read.

The script is **read-only**. It never writes to the cartridge's save RAM. It only touches the
memory-controller registers needed to enable and bank-switch that RAM.

## How it works

1. Loads the Cart Clinic bitstream into the Chromatic FPGA's **volatile SRAM** with
   `openFPGALoader --cable gwu2x --write-sram`. Nothing is written to the Chromatic's flash.
2. Talks to the bitstream over the Chromatic's USB serial port (VID `374E`, PID `0101`,
   115200 baud). Every command and reply is 4 bytes, `struct '<BHB'` = (command id, 16-bit
   address, data):

   | ID   | Command        |
   |------|----------------|
   | 0x01 | Loopback       |
   | 0x02 | Read cart byte |
   | 0x03 | Write cart byte (used only for MBC registers) |

3. Reads the cart header, then dumps `0xA000–0xBFFF` for every RAM bank using standard
   MBC sequences (RAM enable `0x0000=0x0A`, bank select `0x4000`, MBC1 mode `0x6000`).
4. Reads the save **twice** and only writes the `.sav` if both reads are identical.
5. Runs `openFPGALoader --reset`, which reloads the normal Chromatic firmware from flash.
   This happens even if the dump fails. If the reset itself fails, power-cycle the Chromatic.

## Supported cartridges

| Mapper | Status |
|---|---|
| ROM+RAM, MBC1, MBC2, MBC3, MBC5 | ✅ |
| Game Boy Camera, HuC1 | ✅ (untested) |
| MBC6, MBC7, HuC3, TAMA5 | ❌ refused |

For MBC3 carts with a real-time clock (Pokémon Gold/Silver/Crystal), the cart's clock is
latched and read, and appended as the standard 48-byte RTC footer used by VBA-M, BGB, mGBA
and SameBoy. VBA-M crashes on clock-cart saves without this footer. Use `--no-rtc` to get a
plain RAM-only `.sav`.

Tested: Pokémon Gold (MBC3+RTC, 32 KB). The dump passed the game's own save checksum.

## Setup (Windows)

1. Install [Python 3.10+](https://www.python.org/) and the dependency:
   ```bash
   pip install -r requirements.txt
   ```
2. Install ModRetro's **MR Updater**. It installs the GWU2X USB driver needed to reach
   the FPGA. Start it once with the Chromatic connected and open the Cart Clinic tab, so it
   downloads the Cart Clinic firmware.
3. Put these two files in `bin/`. They're not included in this repo because they're ModRetro's
   and third-party files:
   - `cart_clinic_260820.fs`: from the zip MR Updater downloads to
     `%TEMP%\firmware\chromatic\cartclinic\`. If your version has a different filename,
     pass it with `--bitstream`.
   - `openFPGALoader.exe`: bundled inside MR Updater (a PyInstaller app, under
     `lib/openFPGALoader/`). An upstream
     [openFPGALoader](https://github.com/trabucayre/openFPGALoader) build with `gwu2x`
     cable support should also work. Pass a different path with `--loader`.

## Usage

Insert the cartridge and **save your game first**: switching to Cart Clinic mode turns the
running game off.

```bash
python dump_save.py                    # writes "<GAME TITLE>.sav" next to the script
python dump_save.py -o pokemon.sav     # choose the output file
python dump_save.py --header-only      # just identify the cart
```

Existing files are never overwritten. A timestamp is added to the name instead.

## Disclaimer

Not affiliated with or endorsed by ModRetro or Nintendo. Use at your own risk.
Only back up cartridges you own.
