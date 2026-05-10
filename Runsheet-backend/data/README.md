# Data Directory — Runsheet Backend

This directory contains geospatial and reference data files used by the
compliance services. Binary data files (shapefiles) are **not committed to
version control** — use the provided download script to fetch them.

---

## US State Boundary Shapefile

The `StateBoundaryDetector` (IFTA Reporter, Phase 12) uses a US Census TIGER/Line
shapefile for lat/lon → state lookups. This enables offline, low-latency state
boundary detection without relying on external geocoding APIs.

### Required Files

After extraction, the following files must be present in this directory:

| File | Description |
|------|-------------|
| `us_states.shp` | Shape geometry (polygons) |
| `us_states.shx` | Shape index |
| `us_states.dbf` | Attribute table (contains STUSPS field) |
| `us_states.prj` | Coordinate reference system (WGS84) |

### Download Source

**US Census Bureau — Cartographic Boundary Files (2022, 1:500k)**

URL: https://www2.census.gov/geo/tiger/GENZ2022/shp/cb_2022_us_state_500k.zip

This is the 500k (1:500,000) generalized boundary file. It provides a good
balance between accuracy and file size (~4 MB compressed). For higher precision,
the 20m (1:20,000,000) or full-resolution TIGER/Line files are also available.

### Automated Download

Run the download script from the `Runsheet-backend/` directory:

```bash
python scripts/download_shapefile.py
```

This will:
1. Download `cb_2022_us_state_500k.zip` from the Census Bureau
2. Extract the shapefile components
3. Rename them to `us_states.*` in this directory
4. Verify the expected fields are present

### Manual Download

1. Download: https://www2.census.gov/geo/tiger/GENZ2022/shp/cb_2022_us_state_500k.zip
2. Extract the ZIP file
3. Copy and rename the following files into this directory:
   - `cb_2022_us_state_500k.shp` → `data/us_states.shp`
   - `cb_2022_us_state_500k.shx` → `data/us_states.shx`
   - `cb_2022_us_state_500k.dbf` → `data/us_states.dbf`
   - `cb_2022_us_state_500k.prj` → `data/us_states.prj`

### Expected Shapefile Fields

The `StateBoundaryDetector` reads the **STUSPS** field from the `.dbf` attribute
table. This field contains the 2-letter US state/territory postal abbreviation
(e.g., "TX", "CA", "NY").

Other useful fields in the Census TIGER shapefile:

| Field | Description | Example |
|-------|-------------|---------|
| `STUSPS` | 2-letter state abbreviation (primary lookup field) | "TX" |
| `NAME` | Full state name | "Texas" |
| `STATEFP` | 2-digit state FIPS code | "48" |
| `GEOID` | Geographic identifier | "48" |
| `ALAND` | Land area (sq meters) | 676587803750 |
| `AWATER` | Water area (sq meters) | 19006305260 |

### License

**Public Domain** — US Census Bureau data is produced by the federal government
and is not subject to copyright protection in the United States (17 U.S.C. § 105).
There are no licensing restrictions on use, modification, or redistribution.

Source: https://www.census.gov/about/policies/open-gov/open-data.html

### Notes

- The shapefile covers all 50 states, DC, and US territories (PR, GU, VI, AS, MP)
- The `StateBoundaryDetector` filters to continental US bounds by default
  (lat 24°–50°N, lon 125°–66°W) for quick rejection of non-US coordinates
- Grid-cell caching (0.1° cells) means the shapefile is only queried once per
  ~7mi × 5.5mi area, making repeated lookups very fast
