# Open-Meteo forecast fixtures

Raw responses written by `hazelst record open-meteo` (one folder per day).

**Relabeled model ids (J34).** These responses were recorded with
`models=gfs_seamless,ecmwf_ifs025,gem_seamless`. J34 replaced the seamless ids
with the pure global models (`gfs_global`, `gem_global`; decisions row 35), so
every occurrence of `gfs_seamless` and `gem_seamless` in these files (query
`models`, `meta`, the response's variable names, and file names) was renamed
to `gfs_global` and `gem_global`. The values are still the seamless series
(HRRR and HRDPS in the first hours); the tests use them to exercise the
pipeline, not the models' numbers.
