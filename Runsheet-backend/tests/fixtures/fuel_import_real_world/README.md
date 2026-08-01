# Fuel import pilot fixtures

These files model a realistic US fuel-distributor export while containing no
real customer or commercially sensitive data.

- `customer_tanks.csv`: mixed diesel, propane, generator, heating-oil, and
  gasoline tanks, including a ZIP code with a leading zero.
- `orders.csv`: will-call, keep-full, auto-fill, and one-off orders. `PE-9001`
  appears as an original row, a newer ERP revision, and an exact replay.
- `tank_readings.csv`: current, stale, and replayed readings.
- `invalid_*.csv`: operationally unsafe records that validation must reject.

The integration test imports tanks first, then orders and telemetry, matching
the recommended onboarding sequence for a distributor pilot.
