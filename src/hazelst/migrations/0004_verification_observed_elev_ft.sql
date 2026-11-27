-- The elevation of the observation each verification row was scored against:
-- the report's for pns / lsr / ghcn, the NOHRSC pixel center's for nohrsc,
-- NULL for resort / snocountry (measurement site unknown).
ALTER TABLE verification ADD COLUMN observed_elev_ft INT;
