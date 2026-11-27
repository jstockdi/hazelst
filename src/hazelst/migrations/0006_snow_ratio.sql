-- The resort_ratio method (J27, decisions row 26): resort-reported snow per inch of
-- forecast snow water, fitted per mountain (and 'all'), per model (and 'raw_mean' for the
-- models' mean), per summit temperature band (and '' = every band), per nw_upslope side
-- ('yes' | 'no', and '' = both). Rebuilt by skill-learn from lead-day-1 snow days.
CREATE TABLE snow_ratio (
  mountain TEXT, model TEXT, temp_band TEXT, upslope TEXT,
  ratio REAL, n INT, fitted_through DATE,
  PRIMARY KEY (mountain, model, temp_band, upslope)
);
-- Each day's resort_ratio snow beside the model mean's snow_in.
ALTER TABLE forecast_day ADD COLUMN resort_ratio_in REAL;
