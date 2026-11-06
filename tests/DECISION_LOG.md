# T007-PS-035 – Decision Log

| ID | Decision |
|---|---|
| D-01 | Python 3.10+ was selected as the implementation language. |
| D-02 | CSV files are used for storing rider, parcel and operational input data. |
| D-03 | The project is designed as a command-file-driven backend without a frontend. |
| D-04 | The Haversine formula is used to calculate geographical distances. |
| D-05 | Route travel time is calculated using 4 minutes per kilometre. |
| D-06 | Each delivery stop includes 3 minutes of service time. |
| D-07 | COD delivery stops include an additional 5 minutes. |
| D-08 | EXPRESS parcels receive higher assignment priority than STANDARD parcels. |
| D-09 | Parcels are assigned based on eligibility and the lowest added route distance. |
| D-10 | Rider vehicle capacity is validated using both parcel count and weight. |
| D-11 | Delivery events support DELIVERED, FAILED and RESCHEDULED statuses. |
| D-12 | Distance payout uses actual odometer readings only when within the permitted 10% deviation. |
| D-13 | Daily penalties are limited to 25% of daily gross payout. |
| D-14 | Weekly payout includes attendance bonus, EV allowance and applicable TDS. |
| D-15 | Automated tests are used to validate business rules and edge cases. |