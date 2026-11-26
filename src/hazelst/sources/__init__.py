"""Data sources: one module per source, each with `fetch()` (raw) and `normalize()` (rows).

Sources never compute: they fetch raw responses, which the job stores as
`fetch` rows, and parse those responses into normalized rows.
"""
