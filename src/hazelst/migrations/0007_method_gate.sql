-- The method chosen per mountain (J28, decisions row 28): each candidate method's 1d MAE
-- beside raw_mean's on the same days, on the held-out season (or, once the current season
-- has 20 live verified snow days at the mountain, on those). Rewritten by `method choose`
-- and nightly by skill-learn.
CREATE TABLE method_gate (
  mountain TEXT, method TEXT,
  test_from DATE, test_to DATE,
  n INT, mae_in REAL, raw_mean_mae_in REAL,
  chosen INT,
  decided_at TEXT,
  PRIMARY KEY (mountain, method)
);
