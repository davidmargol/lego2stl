# lego2stl
Convert LEGO set inventories into color organized STL and 3MF files using Rebrickable and LDraw. One command, one 3MF per color, ready for your 3D printing workflow.

# LEGO2STL

Convert a LEGO set inventory into organized **STL** and **3MF** files using a set number.

LEGO2STL retrieves the set inventory from Rebrickable, obtains matching part geometry from LDraw, applies the corresponding LEGO/Rebrickable colors, and organizes the result into files that are convenient for 3D-printing workflows.

> **Important:** LEGO2STL does not generate replacement brick geometry. It converts available LDraw geometry. Some parts may require manual inspection or repair before 3D printing.

## Features

- Download a complete set inventory from a Rebrickable set number
- Retrieve matching part geometry from LDraw
- Preserve the quantities required by the original set inventory
- Organize STL files into folders by color
- Create **one 3MF file per color**
- Keep multicolor parts in a separate `Multicolor.3mf`
- Use LEGO/Rebrickable color names and RGB values
- Download the set image when available
- Export `inventory.csv`, `report.json`, and a 3MF manifest
- No MachineBlocks
- No OpenSCAD
- No Bambu Studio dependency
- No printer profile, filament profile, nozzle, support, or G-code settings embedded
- Plain 3MF output that can be opened and configured in your slicer

## Example

```bash
python3 lego2stl.py --set 6835
```

or:

```bash
python3 lego2stl.py --set 6835-1
```

Example output:

```text
lego2stl_output/
└── 6835-1_Saucer_Scout/
    ├── assets/
    │   └── set_image.jpg
    ├── stl/
    │   ├── Black/
    │   │   ├── 2516__QTY-1.stl
    │   │   ├── 3022__QTY-4.stl
    │   │   └── ...
    │   ├── Red/
    │   └── ...
    ├── 3mf/
    │   ├── Black.3mf
    │   ├── Red.3mf
    │   ├── Light_Gray.3mf
    │   ├── Trans_Dark_Blue.3mf
    │   └── Multicolor.3mf
    ├── inventory.csv
    ├── 3mf_manifest.json
    └── report.json
```

### STL output

STL files are kept as individual part files and grouped by color.

The quantity is included in the filename:

```text
3024__QTY-14.stl
```

This means part `3024` is required 14 times in that color.

### 3MF output

The `3mf` directory contains **one 3MF per inventory color**. Each file contains the required number of part instances for that color.

For example:

```text
Black.3mf
Red.3mf
White.3mf
Light_Bluish_Gray.3mf
```

Parts containing multiple colors are placed in:

```text
Multicolor.3mf
```

The generated 3MF files intentionally do **not** contain printer-specific presets. Choose your printer, filament, orientation, supports, layer height, and other print settings in your slicer.

## Requirements

- Python 3.10+
- A free Rebrickable API key
- Internet connection while downloading set/part data

Python dependencies are listed in `requirements.txt`.

## Installation

Clone the repository:

```bash
git clone https://github.com/YOUR_USERNAME/lego2stl.git
cd lego2stl
```

Creating a virtual environment is recommended, especially on Debian/Ubuntu systems using PEP 668:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If `venv` is not installed:

```bash
sudo apt update
sudo apt install python3-venv
```

## Rebrickable API key

Create a Rebrickable account and obtain an API key.

Set it for the current shell:

```bash
export REBRICKABLE_API_KEY="YOUR_API_KEY"
```

To make it persistent on Bash:

```bash
echo 'export REBRICKABLE_API_KEY="YOUR_API_KEY"' >> ~/.bashrc
source ~/.bashrc
```

Do **not** commit your API key to GitHub.

## Usage

Basic usage:

```bash
python3 lego2stl.py --set SET_NUMBER
```

Examples:

```bash
python3 lego2stl.py --set 662203
python3 lego2stl.py --set 6835-1
python3 lego2stl.py --set LIT2009-1
```

Show all available options:

```bash
python3 lego2stl.py --help
```

## How it works

1. The set number is normalized to the Rebrickable set format.
2. The inventory and color information are retrieved from Rebrickable.
3. Matching LDraw geometry is downloaded/resolved for each part.
4. Geometry is converted into STL/3MF-compatible meshes.
5. STL files are grouped into directories by color.
6. One 3MF containing all required instances is created for each color.
7. Inventory and conversion information is written to CSV/JSON reports.

## Limitations

LDraw is primarily a CAD/model library, not a library of meshes specifically optimized for FDM printing. As a result:

- some geometry may not be ideal for direct 3D printing;
- holes, thin details, decorations, or complex parts may require manual work;
- printed/decorated parts may not reproduce the original decoration as printable color geometry;
- availability depends on the corresponding LDraw part data;
- tolerances and clutch fit are not automatically optimized for your printer or filament;
- generated 3MF placement is only an organizational starting point, not a tuned print profile.

Always inspect and slice the model before printing.

## Data sources

LEGO2STL uses data from:

- **Rebrickable** — set inventories, quantities, part/color information
- **LDraw** — LEGO-compatible CAD part geometry

This project is not affiliated with, endorsed by, or sponsored by the LEGO Group, Rebrickable, LDraw.org, or Bambu Lab.

LEGO® is a trademark of the LEGO Group.

## License

**MIT License**

## Contributing

Bug reports, pull requests, part-compatibility improvements, and fixes for unusual LDraw geometry are welcome.

If you report a problem, including the following is especially useful:

```text
Set number:
Part number:
Color:
Console output/error:
Expected result:
Actual result:
```

## Version

**1.0**
