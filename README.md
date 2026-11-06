# T007-PS-035

**Project:** Last-Mile Route Sequencing and Rider Payout Engine  
**Team:** T007  
**Language:** Python 3.10+

## Setup

```bash
python -m venv .venv
```

Activate the environment:

- Windows PowerShell: `.venv\\Scripts\\Activate.ps1`
- macOS/Linux: `source .venv/bin/activate`

Install test dependency:

```bash
pip install -r requirements.txt
```

Run the program:

```bash
python -m src.main
```

Run tests:

```bash
pytest
```

## Project structure

- `src/` – application code
- `tests/` – automated tests
- `data/` – input data files
- `design/` – design documentation
- `commands.txt` – command-file instructions

**Important:** Use the official sample-data-pack README and command format as the source of truth. Do not invent input/output formats.

## Running the command workflow

Run these commands from the repository root (the folder containing `src/`, `ps035/`, and `data/`):

```powershell
python -m pytest -q
python -m src.main
```

The command runner reads and validates the CSV files in `data/`, parses
`data/commands.txt`, and executes its commands in file order. The `LOAD_EVENTS`,
`LOAD_ODOMETER`, and `LOAD_COD` commands report the corresponding input data as
ready because all source CSVs are loaded and validated once at startup.

Generated evaluation files are written to `outputs/`:

- `routes.csv` — planned stops and unassigned parcels
- `daily_payouts.csv` — daily rider payout rows
- `weekly_payouts.csv` — weekly payout rows
- `rider_statements.txt` — printable weekly statements

The command runner does not modify the original input CSV files.
## How to Run the Project

### Requirements
- Python 3.10 or above
- pytest

### Install Dependencies
python -m pip install -r requirements.txt

### Run Automated Tests
python -m pytest -q

### Execute the Project
python -m src.main

## Generated Outputs

The project generates the following files inside the outputs folder:

- routes.csv
- daily_payouts.csv
- weekly_payouts.csv
- rider_statements.txt

## Testing

The current automated test suite contains 669 passing tests.