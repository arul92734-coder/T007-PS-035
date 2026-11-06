# PS-035 – Last-Mile Route Sequencing and Rider Payout Engine — Sample Data Pack

Prepared by 10 SECONDS for Team T007.

## Files

| File | What it holds | Row count |
|---|---|---|
| `hub.csv` | The single hub | 1 data rows |
| `riders.csv` | 8 riders with vehicle type, capacity and default shift | 8 data rows |
| `rider_shifts.csv` | Day-wise roster (a missing row = day off); overrides the default shift | 48 data rows |
| `parcels.csv` | Fresh parcels per pool_date, 250 a day over 6 operating days (Mon–Sat, so a full 6-working-day week can be evaluated; same ~1,500-parcel volume as 5 x 300); rescheduled parcels are NOT repeated — your engine re-pools them | 1500 data rows |
| `delivery_events.csv` | Field delivery log for the 6 days (DELIVERED / FAILED / RESCHEDULED / COMPLAINT) | 1530 data rows |
| `odometer.csv` | Start and end odometer per rider per working day | 48 data rows |
| `cod_deposits.csv` | COD cash deposits at the hub | 45 data rows |
| `commands.txt` | Daily PLAN / LOAD / PAYOUT commands, weekly payout and statements | 87 command lines (excluding comments) |

Row numbers quoted below are 1-based data-row numbers (the header is row 0). Line numbers for `commands.txt` are physical line numbers including comment lines.

## Column reference

Conventions: dates `YYYY-MM-DD`; timestamps `YYYY-MM-DD HH:MM:SS` (24h, IST, no timezone suffix); money in Rs as plain decimals with 2 places (no commas, no symbol); percentages as plain numbers (12.5 means 12.5%); booleans `true`/`false`; IDs uppercase with a fixed prefix. An empty cell means "not applicable" and only appears in columns whose notes say so.

### `hub.csv`

| Column | Type | Units / format | Allowed values | Notes |
|---|---|---|---|---|
| hub_id | string | HUB-nn | — | — |
| hub_name | string | — | — | — |
| lat | decimal | degrees, 6 dp | — | — |
| lon | decimal | degrees, 6 dp | — | — |

### `riders.csv`

| Column | Type | Units / format | Allowed values | Notes |
|---|---|---|---|---|
| rider_id | string | RDR-nn | — | primary key |
| rider_name | string | — | — | — |
| vehicle_type | enum | — | BIKE, EV_BIKE, VAN | — |
| capacity_parcels | int | — | 40 or 120 | — |
| capacity_kg | decimal | kg | 25.0 or 300.0 | — |
| home_hub | string | — | hub.csv | — |
| default_shift_start | time | HH:MM:SS | — | — |
| default_shift_end | time | HH:MM:SS | — | — |

### `rider_shifts.csv`

| Column | Type | Units / format | Allowed values | Notes |
|---|---|---|---|---|
| rider_id | string | — | riders.csv | — |
| shift_date | date | — | — | — |
| shift_start | time | HH:MM:SS | — | route clock starts here at the hub |
| shift_end | time | HH:MM:SS | — | route must be back by this time (+60 min overtime) |

### `parcels.csv`

| Column | Type | Units / format | Allowed values | Notes |
|---|---|---|---|---|
| parcel_id | string | PCL-nnnnn | — | primary key |
| pool_date | date | — | — | day the parcel first enters the pool |
| dest_lat | decimal | degrees, 6 dp | — | — |
| dest_lon | decimal | degrees, 6 dp | — | — |
| weight_kg | decimal | kg, 1 dp | — | — |
| window_start | time | HH:MM:SS | — | ETA must be >= this |
| window_end | time | HH:MM:SS | — | ETA must be <= this |
| priority | enum | — | EXPRESS, STANDARD | a rescheduled STANDARD parcel becomes EXPRESS |
| cod_flag | bool | — | — | 5-min stop |
| cod_amount | decimal | Rs | — | 0.00 when cod_flag is false |

### `delivery_events.csv`

| Column | Type | Units / format | Allowed values | Notes |
|---|---|---|---|---|
| event_id | string | EVT-nnnnn | — | file sorted by event_ts |
| event_date | date | — | — | — |
| parcel_id | string | — | parcels.csv | — |
| rider_id | string | — | riders.csv | authoritative for pay |
| event_ts | timestamp | — | — | — |
| event_type | enum | — | DELIVERED, FAILED, RESCHEDULED, COMPLAINT | — |
| reason_code | string | — | NOT_HOME, WRONG_ADDRESS, RIDER_FAULT (FAILED); free text for COMPLAINT | empty for DELIVERED / RESCHEDULED |
| attempt_no | int | — | 1, 2 | 2 = re-pooled after a reschedule |
| cod_collected | decimal | Rs | — | cash collected on DELIVERED COD parcels |

### `odometer.csv`

| Column | Type | Units / format | Allowed values | Notes |
|---|---|---|---|---|
| rider_id | string | — | riders.csv | — |
| odo_date | date | — | — | — |
| start_km | decimal | km, 2 dp | — | — |
| end_km | decimal | km, 2 dp | — | actual route = end_km - start_km |

### `cod_deposits.csv`

| Column | Type | Units / format | Allowed values | Notes |
|---|---|---|---|---|
| deposit_id | string | DEP-nnnn | — | — |
| rider_id | string | — | riders.csv | — |
| deposit_date | date | — | — | cash day the deposit settles |
| deposit_ts | timestamp | — | — | must be <= 21:00:00 the same day |
| amount | decimal | Rs | — | — |

## Command file format

`commands.txt` holds one command per line in the form `COMMAND_NAME|arg1|arg2|...`. Commands must be processed strictly in file order. Lines starting with `#` are comments and blank lines are ignored. An empty argument (two adjacent `|`) means "not supplied". Every command used in this pack:

| Command | Arguments (in order) | Meaning |
|---|---|---|
| `PLAN` | `date` | Assign that day's pool (fresh + re-pooled parcels) to rostered riders and sequence the stops |
| `PRINT_ROUTE` | `rider_id|date` | Print the rider's planned stop sequence with ETAs |
| `LOAD_EVENTS` | `date` | Load delivery_events.csv rows for the date |
| `LOAD_ODOMETER` | `date` | Load odometer.csv rows for the date |
| `LOAD_COD` | `date` | Load cod_deposits.csv rows for the date |
| `PAYOUT_DAY` | `date` | Compute daily payout for every rider who worked |
| `PAYOUT_WEEK` | `week_start_date` | Compute weekly payout (Mon..Sat) |
| `PRINT_STATEMENT` | `rider_id|week_start_date` | Print the rider's weekly statement |

## Planted scenarios

Every edge case in the brief, mapped to the exact record where it occurs, followed by extra traps.

- **EC-01 Rider at exactly 25.0 kg with a 0.1 kg parcel remaining**
  - 2026-10-15: RDR-06 (BIKE, 25 kg) is the only rider on shift before 10:00; eight parcels with windows ending 09:30 weigh exactly 25.0 kg in total; the 0.1 kg parcel (window to 09:31) cannot be added -> UNASSIGNED — parcels.csv row 751 (PCL-00751); parcels.csv row 752 (PCL-00752); parcels.csv row 753 (PCL-00753); parcels.csv row 754 (PCL-00754); parcels.csv row 755 (PCL-00755); parcels.csv row 756 (PCL-00756); parcels.csv row 757 (PCL-00757); parcels.csv row 758 (PCL-00758); parcels.csv row 759 (PCL-00759)
- **EC-02 EXPRESS parcel whose only feasible rider's first stop is exactly 09:30**
  - 2026-10-13: the day's only EXPRESS parcel is 7.50 km from the hub; only RDR-08 starts at 09:00 (all others 10:00) -> its first stop ETA = 09:00 + 30 min = exactly 09:30 — parcels.csv row 251 (PCL-00251); rider_shifts.csv row 16 (RDR-08)
- **EC-03 Route returning to hub exactly at shift end**
  - 2026-10-14: RDR-03 works 09:00–10:00; its only feasible parcels sit on one ray: hub -> 3.00 km -> +3.75 km -> 6.75 km back = 13.50 km = 54 min + 2 x 3 min service = 60 min -> back at the hub exactly at 10:00:00 — parcels.csv row 501 (PCL-00501); parcels.csv row 502 (PCL-00502); rider_shifts.csv row 19 (RDR-03)
- **EC-04 Actual odometer exactly 10% above planned**
  - RDR-03 on 2026-10-14: planned route 13.50 km (EC-03), odometer 34129.40 -> 34144.25 = 14.85 km = exactly 10% above -> actual is 'within 10%'? — odometer.csv row 19 (RDR-03)
- **EC-05 Rider delivering 30 parcels, 29 on first attempt (96.7%)**
  - RDR-02 on 2026-10-16: 30 DELIVERED events, one of them (PCL-00877) on attempt 2 after a RESCHEDULE the day before -> 29/30 = 96.7% first-attempt — delivery_events.csv row 1021 (EVT-01021); delivery_events.csv row 1213 (EVT-01213)
- **EC-06 Penalties exceeding 25% cap on a low-gross day**
  - RDR-07 on 2026-10-17 (shift 15:00–18:00): 4 parcels, 2 DELIVERED, 2 FAILED RIDER_FAULT (Rs 50 each) and a customer COMPLAINT (Rs 100) -> penalties far above 25% of a ~Rs 36 + distance gross — delivery_events.csv row 1367 (EVT-01367); delivery_events.csv row 1389 (EVT-01389); delivery_events.csv row 1408 (EVT-01408); delivery_events.csv row 1419 (EVT-01419); delivery_events.csv row 1441 (EVT-01441)
- **EC-07 COD deposit of exactly Rs 2,000 short**
  - RDR-01 on 2026-10-14: COD deposit exactly Rs 2000.00 below the cash collected that day -> deducted, but NOT > Rs 2,000 so the weekly payout is not frozen — cod_deposits.csv row 22 (DEP-0022)
- **EC-08 Week with 6 working days where one day has only a FAILED attempt**
  - RDR-04 is rostered all 6 days; on 2026-10-17 (shift 17:00–18:00) its only event is one FAILED NOT_HOME attempt -> is that a working day for the Rs 500 attendance bonus? — delivery_events.csv row 1456 (EVT-01456); rider_shifts.csv row 44 (RDR-04)
- **Extra traps (not in the brief, but realistic):**
  - RDR-08 on 2026-10-12: COD shortfall Rs 2600.00 (> Rs 2,000) -> weekly payout frozen pending review — cod_deposits.csv row 3 (DEP-0003)
  - COD deposits at exactly 21:00:00 (in time) and at 21:04:00 (after the 21:00 cut-off) — cod_deposits.csv row 30 (DEP-0030); cod_deposits.csv row 16 (DEP-0016)
  - PCL-00435 is RESCHEDULED on two consecutive days -> becomes RTO and leaves the pool — delivery_events.csv row 417 (EVT-00417); delivery_events.csv row 625 (EVT-00625)
  - FAILED event with reason_code CUSTOMER_BUSY — not one of NOT_HOME, WRONG_ADDRESS, RIDER_FAULT -> flag it; it earns no Rs 10 failed-attempt pay — delivery_events.csv row 199 (EVT-00199)
  - PCL-00033 delivered at 13:04:35 after its window end -> Rs 20 late penalty — delivery_events.csv row 18 (EVT-00018)

## What is NOT in this pack

Expected outputs are not provided for this set. Validate your engine with your own test suite against the rules in your brief (BR-01 … BR-nn). A separate hidden input set with the same schema will be used at final evaluation; outputs are compared field-for-field, so follow the column formats above exactly in your own output.

## Output format expected at evaluation

Produce these CSV files (header row, same conventions as above, rows sorted as stated). Where the brief is silent on a format detail the choice below is binding for evaluation.

### `routes.csv`

One row per stop per PRINT_ROUTE, in stop order; unassigned parcels listed with rider_id UNASSIGNED.

Columns, in order: `date`, `rider_id`, `seq`, `parcel_id`, `leg_km`, `eta`, `window_start`, `window_end`, `cumulative_kg`, `return_to_hub_ts`, `planned_route_km`

### `daily_payouts.csv`

One row per rider per PAYOUT_DAY.

Columns, in order: `date`, `rider_id`, `delivered`, `failed_paid`, `parcel_pay`, `planned_km`, `actual_km`, `km_used`, `odometer_flag`, `distance_pay`, `overtime_pay`, `incentive`, `first_attempt_bonus`, `gross`, `penalties_raw`, `penalties_capped`, `cod_shortfall`, `net`

### `weekly_payouts.csv`

One row per rider per PAYOUT_WEEK.

Columns, in order: `week_start`, `rider_id`, `working_days`, `daily_net_total`, `attendance_bonus`, `ev_allowance`, `weekly_gross`, `tds`, `net_rounded`, `frozen`

Binding choices: distances are haversine (Earth radius 6371.0 km) rounded to 2 dp per leg; 4 min per km; ETAs printed as HH:MM:SS; a rider waits at a stop if the ETA is before window_start; `odometer_flag` true when actual is more than 10% above planned; `frozen` true/false.
