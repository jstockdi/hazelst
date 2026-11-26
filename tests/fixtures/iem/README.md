# IEM fixtures

Iowa Environmental Mesonet responses recorded for J12 (`source-obs`) and J22
(`season-backfill`). Request header: `User-Agent: hazelst-forecast (github.com/jstockdi/hazelst)`.
All files are gzip-compressed and untrimmed. Endpoint notes are in
[docs/design/data-sources.md](../../../docs/design/data-sources.md).

| File | Source URL | HTTP | Fetched (UTC) |
|------|-----------|------|---------------|
| `lsr_btv_aly_gyx_2026-02-01_2026-02-08.geojson.gz` | `https://mesonet.agron.iastate.edu/geojson/lsr.php?wfos=BTV,ALY,GYX&sts=202602010000&ets=202602080000` | 200 | 2026-11-26T14:18:11Z |
| `lsr_btv_aly_gyx_2026-11-19_2026-11-26_empty.geojson.gz` | `https://mesonet.agron.iastate.edu/geojson/lsr.php?wfos=BTV,ALY,GYX&sts=202611190000&ets=202611261200` (no reports in the window) | 200 | 2026-11-26T14:18:04Z |
| `afos_list_PNSBTV_2026-02-07.json.gz` | `https://mesonet.agron.iastate.edu/api/1/nws/afos/list.json?pil=PNSBTV&date=2026-02-07` | 200 | 2026-11-26T14:18:40Z |
| `nwstext_202602071335-KBTV-NOUS41-PNSBTV.txt.gz` | `https://mesonet.agron.iastate.edu/api/1/nwstext/202602071335-KBTV-NOUS41-PNSBTV` | 200 | 2026-11-26T14:18:44Z |
| `nwstext_202602071323-KALY-NOUS41-PNSALY.txt.gz` | `https://mesonet.agron.iastate.edu/api/1/nwstext/202602071323-KALY-NOUS41-PNSALY` | 200 | 2026-11-26T14:18:59Z |
| `nwstext_202602071650-KGYX-NOUS41-PNSGYX.txt.gz` | `https://mesonet.agron.iastate.edu/api/1/nwstext/202602071650-KGYX-NOUS41-PNSGYX` | 200 | 2026-11-26T14:18:58Z |

The three PNS texts cover both metadata report types: `SNOW_24` (BTV, GYX) and
`SNOW` storm totals (ALY). The GYX text includes a ` null Portland Jetport`
place name, which the parser must clean.
