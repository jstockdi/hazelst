-- SnoCountry history is one row per mountain per report date; writes upsert on it.
CREATE UNIQUE INDEX observation_snocountry_day ON observation (mountain, source, obs_date)
  WHERE source = 'snocountry';
