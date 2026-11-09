# Project Demonstration Guide

## Project Details
- **Project ID:** PS-035
- **Team ID:** T007
- **Project Title:** Last-Mile Route Sequencing and Rider Payout Engine
- **Programming Language:** Python

## 1. Project Execution

Run the following command from the project root:

```bash
python -m src.main
```

This executes the project using the configured sample data and command file.

## 2. Automated Testing

Run the test suite using:

```bash
python -m pytest -v
```

The recorded test run completed with 669 passing tests.

## 3. Generated Output Files

The project generates:

- `routes.csv` – planned delivery routes and stop sequences.
- `daily_payouts.csv` – daily rider payout details.
- `weekly_payouts.csv` – weekly payout summaries.
- `rider_statements.txt` – rider payout statements.

## 4. Features to Demonstrate

1. Rider and parcel data processing.
2. Parcel assignment and route sequencing.
3. ETA calculation.
4. Delivery event processing.
5. Daily payout calculation.
6. Weekly payout calculation.
7. Automated test execution.

## 5. Conclusion

The project provides a Python-based backend solution for last-mile delivery planning, route sequencing and rider payout management.