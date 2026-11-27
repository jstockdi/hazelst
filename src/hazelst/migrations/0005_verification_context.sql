-- Every non-resort source's value for the scored mountain-day (J26, decisions row 24):
-- a JSON list of {source, place, snow_in, elev_ft, distance_mi, hours, ending_at} for
-- the NOHRSC pixel and each PNS / LSR / GHCN report within 20 mi. Stored beside the
-- score, never used as the observation. NULL on rows scored before this migration.
ALTER TABLE verification ADD COLUMN context JSON;
-- The context reads point reports by the time their span ends, one day at a time.
CREATE INDEX snow_report_ending_at ON snow_report (ending_at);
