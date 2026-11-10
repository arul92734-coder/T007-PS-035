# Project Architecture – PS-035

## 1. Project Overview
The Last-Mile Route Sequencing and Rider Payout Engine is a Python-based backend application designed to automate parcel assignment, delivery route planning, event processing and rider payout calculations.

## 2. Technology Stack
- Programming Language: Python
- Input Format: CSV files
- Output Format: CSV and TXT files
- Testing Framework: Pytest
- Version Control: Git and GitHub

## 3. Project Structure

### ps035 – Core Business Logic
- `assignment.py` – Assigns parcels to eligible riders.
- `command_parser.py` – Reads and interprets commands.
- `daily_payout.py` – Calculates daily rider earnings.
- `delivery_events.py` – Processes delivery status events.
- `distance.py` – Calculates geographical distances.
- `eta.py` – Estimates delivery arrival times.
- `loader.py` – Loads and validates input datasets.
- `route_sequencing.py` – Determines the delivery stop sequence.
- `weekly_payout.py` – Calculates weekly rider payouts.

### src – Application Layer
- `main.py` – Main command-line execution file.
- `parcel.py` – Parcel data model.
- `payout.py` – Payout-related model.
- `rider.py` – Rider data model.
- `route.py` – Route-related model.

### Other Directories
- `data/` – Official input CSV files and commands.
- `outputs/` – Generated route and payout reports.
- `tests/` – Automated test cases.
- `design/` – Project design documentation.

## 4. Application Workflow
1. Load and validate input CSV datasets.
2. Process delivery events and parcel availability.
3. Read commands from the command file.
4. Assign parcels to eligible riders.
5. Sequence delivery stops and calculate ETAs.
6. Process daily delivery and payout information.
7. Calculate weekly payouts and deductions.
8. Generate CSV reports and rider statements.

## 5. Output Files
- `routes.csv` – Planned delivery routes.
- `daily_payouts.csv` – Daily rider payout details.
- `weekly_payouts.csv` – Weekly payout summaries.
- `rider_statements.txt` – Individual rider payout statements.

## 6. Execution
Run the application from the repository root:

`python -m src.main`

Run automated tests:

`python -m pytest -v`