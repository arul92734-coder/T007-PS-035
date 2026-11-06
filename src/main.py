"""Command-line runner for the PS-035 last-mile logistics engine.

Run from the repository root with: python -m src.main
The runner reads the official CSV inputs and commands, executes commands in
file order, and writes evaluation outputs under ``outputs/``.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

from ps035.assignment import assign_parcels
from ps035.command_parser import parse_commands
from ps035.daily_payout import PayoutContext, calculate_daily_payouts
from ps035.delivery_events import EventLog
from ps035.loader import load_dataset
from ps035.weekly_payout import calculate_weekly_payouts

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "outputs"

ROUTE_FIELDS = [
    "date", "rider_id", "seq", "parcel_id", "leg_km", "eta",
    "window_start", "window_end", "cumulative_kg", "return_to_hub_ts",
    "planned_route_km",
]
DAILY_FIELDS = [
    "date", "rider_id", "delivered", "failed_paid", "parcel_pay", "planned_km",
    "actual_km", "km_used", "odometer_flag", "distance_pay", "overtime_pay",
    "incentive", "first_attempt_bonus", "gross", "penalties_raw",
    "penalties_capped", "cod_shortfall", "net",
]
WEEKLY_FIELDS = [
    "week_start", "rider_id", "working_days", "daily_net_total", "attendance_bonus",
    "ev_allowance", "weekly_gross", "tds", "net_rounded", "frozen",
]


def _clock(value: timedelta) -> str:
    seconds = int(value.total_seconds()) % (24 * 60 * 60)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run() -> int:
    print("T007-PS-035 | Last-Mile Route Sequencing and Rider Payout Engine")
    print("Loading and validating official CSV data...")
    ds = load_dataset(DATA_DIR)
    event_log = EventLog.from_dataset(ds)
    payout_context = PayoutContext.from_dataset(ds, event_log)
    commands = parse_commands(DATA_DIR / "commands.txt")

    OUTPUT_DIR.mkdir(exist_ok=True)
    route_rows: list[dict[str, str]] = []
    daily_rows: list[dict[str, str]] = []
    weekly_rows: list[dict[str, str]] = []
    statements: list[str] = []
    plans: dict[date, object] = {}
    daily_results_by_date: dict[date, tuple] = {}
    weekly_results: dict[date, dict[str, object]] = {}

    print(f"Validated {len(ds.riders)} riders, {len(ds.parcels)} parcels, "
          f"{len(ds.events)} delivery events and {len(commands)} commands.")

    for command in commands:
        name, args = command.name, command.args
        if name == "PLAN":
            day = date.fromisoformat(args[0])
            pool = event_log.pool_for_date(day)
            result = assign_parcels(ds.riders, ds.hubs, pool, ds.shifts, day)
            plans[day] = result
            assigned = sum(a.parcel_count for a in result.assignments)
            # The evaluation route file also records parcels that could not be
            # assigned, using the required UNASSIGNED rider identifier.
            for item in result.unassigned:
                route_rows.append({
                    "date": day.isoformat(), "rider_id": "UNASSIGNED", "seq": "",
                    "parcel_id": item.parcel_id, "leg_km": "", "eta": "",
                    "window_start": "", "window_end": "", "cumulative_kg": "",
                    "return_to_hub_ts": "", "planned_route_km": "",
                })
            print(f"PLAN {day}: assigned {assigned}/{len(pool)} parcels; "
                  f"unassigned {len(result.unassigned)}")

        elif name == "PRINT_ROUTE":
            rider_id, day_text = args
            day = date.fromisoformat(day_text)
            if day not in plans:
                raise RuntimeError(f"line {command.line_number}: PLAN {day} must run before PRINT_ROUTE")
            assignment = plans[day].for_rider(rider_id)
            if assignment is None:
                print(f"ROUTE {day} {rider_id}: OFF ROSTER")
                continue
            route = assignment.route
            print(f"ROUTE {day} {rider_id}: {len(route.stops)} stops, "
                  f"{route.total_km:.2f} km, return {_clock(route.return_time)}")
            cumulative_kg = 0
            for stop in route.stops:
                parcel = payout_context.parcels[stop.parcel_id]
                cumulative_kg += parcel.weight_kg
                route_rows.append({
                    "date": day.isoformat(), "rider_id": rider_id, "seq": str(stop.seq),
                    "parcel_id": stop.parcel_id, "leg_km": f"{stop.leg_km:.2f}",
                    "eta": _clock(stop.eta), "window_start": _clock(stop.window_start),
                    "window_end": _clock(stop.window_end), "cumulative_kg": f"{cumulative_kg:.1f}",
                    "return_to_hub_ts": _clock(route.return_time),
                    "planned_route_km": f"{route.total_km:.2f}",
                })
            if not route.stops:
                route_rows.append({"date": day.isoformat(), "rider_id": rider_id,
                                   "seq": "", "parcel_id": "", "leg_km": "", "eta": "",
                                   "window_start": "", "window_end": "", "cumulative_kg": "0.0",
                                   "return_to_hub_ts": _clock(route.return_time),
                                   "planned_route_km": f"{route.total_km:.2f}"})

        elif name in {"LOAD_EVENTS", "LOAD_ODOMETER", "LOAD_COD"}:
            # All three CSVs are validated and loaded once at startup. These
            # commands preserve the required stream order and report readiness.
            print(f"{name} {args[0]}: source data already loaded and validated")

        elif name == "PAYOUT_DAY":
            day = date.fromisoformat(args[0])
            plan = plans.get(day)
            planned_routes = ({a.rider_id: a.route for a in plan.assignments} if plan else {})
            results = calculate_daily_payouts(
                payout_context, day, planned_routes=planned_routes
            )
            daily_results_by_date[day] = results
            for result in results:
                daily_rows.append(result.to_csv_row())
            print(f"PAYOUT_DAY {day}: calculated {len(results)} rider payouts")

        elif name == "PAYOUT_WEEK":
            week_start = date.fromisoformat(args[0])
            six_days = tuple(week_start + timedelta(days=i) for i in range(6))
            daily_results = tuple(r for day in six_days
                                  for r in daily_results_by_date.get(day, ()))
            results = calculate_weekly_payouts(
                payout_context, week_start, daily_results=daily_results
            )
            weekly_results[week_start] = {r.rider_id: r for r in results}
            for result in results:
                weekly_rows.append(result.to_csv_row())
            print(f"PAYOUT_WEEK {week_start}: calculated {len(results)} weekly payouts")

        elif name == "PRINT_STATEMENT":
            rider_id, week_text = args
            week_start = date.fromisoformat(week_text)
            result = weekly_results.get(week_start, {}).get(rider_id)
            if result is None:
                raise RuntimeError(
                    f"line {command.line_number}: PAYOUT_WEEK {week_start} must run before PRINT_STATEMENT {rider_id}"
                )
            block = list(result.statement_lines())
            statements.extend(block + [""])
            print(f"PRINT_STATEMENT {rider_id} {week_start}: ready")

    _write_csv(OUTPUT_DIR / "routes.csv", ROUTE_FIELDS, route_rows)
    _write_csv(OUTPUT_DIR / "daily_payouts.csv", DAILY_FIELDS, daily_rows)
    _write_csv(OUTPUT_DIR / "weekly_payouts.csv", WEEKLY_FIELDS, weekly_rows)
    (OUTPUT_DIR / "rider_statements.txt").write_text("\n".join(statements), encoding="utf-8")

    print("\nExecution complete.")
    print(f"Outputs written to: {OUTPUT_DIR}")
    print(f"  routes.csv: {len(route_rows)} rows")
    print(f"  daily_payouts.csv: {len(daily_rows)} rows")
    print(f"  weekly_payouts.csv: {len(weekly_rows)} rows")
    print(f"  rider_statements.txt: {len(statements)} lines")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
