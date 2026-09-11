# Snowflake Iceberg Tables – Option 1 (Snowflake as Catalog, External Volume Storage)

This document explains **Option 1**: using **Snowflake‑managed Iceberg tables** with data stored in **your cloud object storage** (S3/ADLS/GCS) so that:

- Snowflake fully owns the table (DML, governance, Time Travel, etc.).
- Data is stored as **Iceberg + Parquet files** in your bucket.
- Other engines like **DuckDB** can read those files directly from storage.

At the end, **Section 9** provides a deep comparison of all Snowflake table types (permanent, external, hybrid, and others), diagrams, and a master fit matrix explaining **why they are not suitable** for direct file-level access from DuckDB and **why Iceberg on an external volume is required**.

---

## Executive one-pager *(present this page)*

### The goal

> **Snowflake writes the table. DuckDB reads the same data directly from our cloud bucket — no Snowflake at query time.**

---

### Five things we need (all at once)

| # | Requirement |
|---|---|
| 1 | Open **Parquet + Iceberg** files in **our** S3 / ADLS / GCS bucket |
| 2 | **Snowflake full DML** (INSERT / UPDATE / DELETE / MERGE) |
| 3 | **ACID** table semantics (safe reads while Snowflake writes) |
| 4 | **DuckDB** scans files locally — no Snowflake compute per query |
| 5 | **One table** — no nightly UNLOAD / duplicate copies |

---

### Snowflake table types at a glance

```text
                        Can DuckDB read YOUR bucket directly?
                        ─────────────────────────────────────

  Permanent / Transient / Temporary          ✗  NO
  ┌──────────────┐                           (micro-partitions hidden
  │  Snowflake   │                            inside Snowflake — no
  │ micro-parts  │──► SQL only                s3:// path exists)
  └──────────────┘

  External table (plain Parquet)             ~  PARTIAL
  ┌──────────────┐                           DuckDB CAN read Parquet,
  │  Your bucket │◄── Snowflake READ only     but Snowflake CANNOT
  │  *.parquet   │                            write safely (no ACID)
  └──────────────┘

  Hybrid / Dynamic / Event                   ✗  NO
  ┌──────────────┐                           Snowflake-internal or
  │  Snowflake   │──► SQL only                logical layers only
  └──────────────┘

  Iceberg + SNOWFLAKE_MANAGED                ~  PARTIAL
  ┌──────────────┐                           Open format, but files
  │  Snowflake   │                            live in Snowflake storage,
  │  storage     │                            not YOUR bucket
  └──────────────┘

  ★ Iceberg + EXTERNAL VOLUME (Option 1)     ✓  YES  ← chosen path
  ┌──────────────┐     writes        ┌──────────────┐
  │  Snowflake   │ ────────────────► │  YOUR bucket │
  │  (catalog +  │                   │  metadata/   │
  │   DML)       │                   │  data/*.pq   │
  └──────────────┘                   └──────┬───────┘
                                            │ iceberg_scan
                                            ▼
                                     ┌──────────────┐
                                     │   DuckDB     │
                                     │ (no Snowflake│
                                     │  at query)   │
                                     └──────────────┘
```

---

### Fit matrix (one slide)

| Table type | Where data lives | Snowflake writes? | DuckDB reads your bucket? | Verdict |
|---|---|---|---|---|
| **Permanent / Transient / Temp** | Snowflake micro-partitions | Yes | No | Not suitable |
| **External** | Your Parquet/CSV | Read-only | Yes (raw files) | Not suitable — no ACID writes |
| **Hybrid** | Snowflake-internal | Yes | No | Not suitable |
| **Dynamic / Event / Directory** | Underlying source | Varies | No | Not suitable |
| **Iceberg + SNOWFLAKE_MANAGED** | Snowflake storage | Yes | No (not your bucket) | Not suitable for this pattern |
| **Iceberg + external volume** | **Your bucket (Iceberg)** | **Yes** | **Yes** | **Use this** |

---

### Why common tables fail (talk track)

| Type | One-line reason it fails |
|---|---|
| **Permanent** | Data is locked in Snowflake’s proprietary format — DuckDB has no file path to open |
| **External** | DuckDB can read files, but Snowflake cannot safely **write** shared files without Iceberg |
| **Hybrid** | Same as permanent — storage never leaves Snowflake |
| **Iceberg (managed storage)** | Right format, wrong location — files are not in a bucket DuckDB owns |

**UNLOAD workaround?** Export Parquet on a schedule → stale snapshot, double storage, no live shared table.

---

### Why Iceberg (not just Parquet folders)

```text
  Plain Parquet folder              Iceberg table
  ─────────────────────             ─────────────────────────────
  part-000.parquet                  metadata/v2.metadata.json  ← snapshot
  part-001.parquet        vs.       data/*.parquet             ← immutable files
  part-002.parquet                  manifest lists             ← ACID commits

  ✗ Which files are current?        ✓ Atomic snapshot swap
  ✗ Unsafe concurrent writes        ✓ Safe Snowflake write + DuckDB read
  ✗ No schema versioning            ✓ Schema evolution in metadata
```

---

### Recommended pattern (Option 1)

```text
  WRITE (Snowflake)                    READ (DuckDB)
  ─────────────────                    ────────────────
  INSERT / MERGE / compaction    →     iceberg_scan('s3://…/metadata/vN.json')
  CATALOG = 'SNOWFLAKE'                or Iceberg REST catalog
  EXTERNAL_VOLUME = your bucket        httpfs + iceberg extensions

  Rule: Snowflake = single writer │ DuckDB / others = readers
```

| Role | Technology |
|---|---|
| Catalog + DML | Snowflake (`CREATE ICEBERG TABLE … CATALOG = 'SNOWFLAKE'`) |
| Physical storage | Your S3 / ADLS / GCS (`EXTERNAL_VOLUME`) |
| Local / KPI analytics | DuckDB (`iceberg_scan` or REST `ATTACH`) |

**Bottom line:** Only **Snowflake-managed Iceberg on an external volume** gives Snowflake full table ownership **and** DuckDB direct file access from **your** bucket.

*Details: Section 9 (deep dive) · Implementation: Sections 2–7*

---

## 1. High‑level architecture

### 1.1 Components

- **Cloud object storage** (S3/ADLS/GCS)
  - Bucket or container you own, e.g. `s3://my-lakehouse-bucket/tables/`.
- **External Volume (Snowflake)**
  - Snowflake object that represents a writable storage location in your bucket.
- **Snowflake‑managed Iceberg table**
  - `CREATE ICEBERG TABLE ... CATALOG = 'SNOWFLAKE' EXTERNAL_VOLUME = ...`.
  - Snowflake is the Iceberg catalog and write authority.
- **DuckDB with Iceberg + httpfs extensions**
  - Reads the same Iceberg table **directly from the bucket** (metadata + Parquet).

### 1.2 Data flow

1. You create an **external volume** in Snowflake pointing to your bucket prefix.[cite:31]
2. You create an **Iceberg table** using that external volume with `CATALOG = 'SNOWFLAKE'`.[cite:31]
3. Snowflake DML writes Parquet + Iceberg metadata files into your bucket.[cite:31]
4. DuckDB connects to the bucket and **scans the Iceberg table directly** using `iceberg_scan(...)` or an Iceberg REST catalog.[cite:140]

Conceptual diagram:

```text
        Snowflake account (on AWS/Azure/GCP)
        ┌───────────────────────────────────┐
        │  Snowflake catalog & services    │
        │   - ICEBERG TABLE metadata       │
        │   - Snapshots, manifests         │
        └──────────────┬───────────────────┘
                       │   DML (INSERT/UPDATE/DELETE)
                       │   reads/writes
                       ▼
        Cloud Object Storage (your bucket)
        ┌───────────────────────────────────┐
        │  s3://my-lakehouse-bucket/tables/│
        │    └ orders/                     │
        │        ├ metadata/vN.metadata.json
        │        └ data/*.parquet          │
        └───────────────────────────────────┘
                       ▲
                       │  direct scan (Iceberg + Parquet)
                       │
                 DuckDB (local)
```

---

## 2. Step 1 – Prepare cloud storage and identity

### 2.1 Bucket and prefix

- Create or choose a bucket/container, e.g.:
  - AWS S3: `my-lakehouse-bucket`
  - Azure Blob: container `lakehouse`
  - GCS: bucket `my-lakehouse-bucket`
- Reserve a prefix for Snowflake Iceberg tables, e.g. `tables/`.

Snowflake will write:

- `.../tables/orders/metadata/...`
- `.../tables/orders/data/...`[cite:31]

### 2.2 Cloud identity for Snowflake

Create an identity that Snowflake can assume/use to access your bucket with **read/write** permissions on that prefix:

- **AWS** – IAM role with:
  - `s3:ListBucket` on the bucket.
  - `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on `bucket/tables/*`.
- **Azure** – service principal / managed identity with `Storage Blob Data Contributor` on the container or prefix.
- **GCP** – service account with `Storage Object Admin` on the bucket.[cite:46]

You will reference this identity in the `CREATE EXTERNAL VOLUME` statement.[cite:46]

---

## 3. Step 2 – Create the external volume in Snowflake

The **external volume** is the binding between Snowflake and your bucket.[cite:31]

### 3.1 Example: S3 external volume

```sql
CREATE OR REPLACE EXTERNAL VOLUME my_ext_vol
STORAGE_LOCATIONS = (
  (
    NAME               = 'primary'
    STORAGE_PROVIDER   = 'S3'
    STORAGE_BASE_URL   = 's3://my-lakehouse-bucket/tables/'
    STORAGE_AWS_ROLE_ARN = 'arn:aws:iam::123456789012:role/snowflake-iceberg-role'
  )
)
ALLOW_WRITES = TRUE;
```

Key points:[cite:46][cite:47][cite:86]

- `STORAGE_BASE_URL` – root prefix for all tables using this volume.
- `STORAGE_AWS_ROLE_ARN` – role Snowflake will assume to read/write.
- `ALLOW_WRITES = TRUE` – required so Snowflake‑managed Iceberg tables can write data and metadata.

You can validate the setup with:

```sql
DESC EXTERNAL VOLUME my_ext_vol;
```

For Azure/GCS, use `STORAGE_PROVIDER = 'AZURE'` or `'GCS'` and the corresponding identity fields.[cite:46][cite:86]

---

## 4. Step 3 – Create the Snowflake Iceberg table

Now you define the table that will live as Iceberg files in your bucket.

### 4.1 Basic DDL

```sql
CREATE OR REPLACE ICEBERG TABLE analytics.orders_iceberg (
  order_id     STRING,
  customer_id  STRING,
  amount       NUMBER(12,2),
  order_date   DATE
)
CATALOG         = 'SNOWFLAKE'
EXTERNAL_VOLUME = 'my_ext_vol'
BASE_LOCATION   = 'orders/'
PARTITION BY    (order_date);
```

Explanation:[cite:31][cite:34][cite:86]

- `ICEBERG TABLE` – declares this as an Iceberg table type.
- `CATALOG = 'SNOWFLAKE'` – Snowflake is the Iceberg catalog (it tracks snapshots, schemas, manifests).[cite:31]
- `EXTERNAL_VOLUME = 'my_ext_vol'` – use the external volume you created.
- `BASE_LOCATION = 'orders/'` – Snowflake writes under `s3://my-lakehouse-bucket/tables/orders/`.
- `PARTITION BY (order_date)` – Iceberg partition spec for pruning and performance.[cite:31][cite:36]

After creation, the bucket structure looks like:

```text
s3://my-lakehouse-bucket/tables/orders/
  ├─ metadata/
  │    ├─ v1.metadata.json
  │    ├─ v2.metadata.json
  │    └─ ...
  └─ data/
       ├─ 00000-....parquet
       ├─ 00001-....parquet
       └─ ...
```

### 4.2 CTAS pattern (optional)

To create and load in one shot:

```sql
CREATE OR REPLACE ICEBERG TABLE analytics.orders_iceberg
CATALOG         = 'SNOWFLAKE'
EXTERNAL_VOLUME = 'my_ext_vol'
BASE_LOCATION   = 'orders/'
AS
SELECT order_id, customer_id, amount, order_date
FROM analytics.orders_source;
```

This writes all data and metadata as a valid Iceberg table in your bucket while Snowflake manages the catalog.[cite:31][cite:34]

---

## 5. Step 4 – Use the table from Snowflake

You operate on `analytics.orders_iceberg` like any other table.

### 5.1 DML examples

```sql
-- Insert rows
INSERT INTO analytics.orders_iceberg
VALUES
  ('O1', 'C1', 100.00, '2026-01-01'),
  ('O2', 'C2',  50.00, '2026-01-02');

-- Update rows
UPDATE analytics.orders_iceberg
SET amount = amount * 1.1
WHERE order_date >= '2026-01-01';

-- Delete rows
DELETE FROM analytics.orders_iceberg
WHERE amount < 10;
```

Each DML statement creates a **new Iceberg snapshot**:

- A new metadata file under `metadata/`.
- Updated manifest lists and manifests.
- References to new/invalidated Parquet files.

Snowflake handles all Iceberg semantics and snapshot management for you.[cite:31][cite:33]

---

## 6. Step 5 – Read the same table from DuckDB (no Snowflake)

From DuckDB’s perspective, this is just an Iceberg table in your bucket.

### 6.1 Install and load DuckDB extensions

In DuckDB (CLI or via Python/R):

```sql
INSTALL httpfs;
INSTALL iceberg;
LOAD httpfs;
LOAD iceberg;
```

These extensions provide:

- `httpfs` – S3/HTTP/ADLS/GCS object storage access.
- `iceberg` – understanding of Iceberg metadata + Parquet layout.[cite:140][cite:146]

### 6.2 Configure cloud credentials in DuckDB

Example (S3, using default AWS credential chain):

```sql
CREATE SECRET s3_iceberg_secret (
  TYPE s3,
  PROVIDER credential_chain
);
```

This tells DuckDB to use your AWS credentials (env vars, profile, role, etc.) for S3 access.[cite:146][cite:147]

### 6.3 Scan the Iceberg table by metadata path

You can now point DuckDB directly at the Iceberg metadata file:

```sql
SELECT customer_id,
       SUM(amount) AS total_spend
FROM iceberg_scan('s3://my-lakehouse-bucket/tables/orders/metadata/v2.metadata.json')
WHERE order_date >= DATE '2026-01-01'
GROUP BY customer_id;
```

What happens internally:[cite:140][cite:152][cite:193]

1. `iceberg_scan(...)` reads `v2.metadata.json` from your bucket.
2. That metadata file points to the current snapshot and manifest list.
3. Manifests list all Parquet data files and stats.
4. DuckDB prunes files and reads only the necessary Parquet files via `httpfs`.

No Snowflake compute or APIs are involved at query time; you’re operating purely on **your bucket**.

---

## 7. Optional – Use an Iceberg REST catalog instead of raw paths

Rather than hard‑coding `metadata/vN.metadata.json`, you can:

1. Expose your Iceberg tables via an **Iceberg REST catalog** (e.g. Snowflake Open Catalog / Polaris or another REST implementation).
2. Attach that catalog in DuckDB.

Example (generic REST catalog):

```sql
CREATE SECRET iceberg_rest_secret (
  TYPE iceberg,
  CLIENT_ID 'my_client',
  CLIENT_SECRET 'my_secret',
  OAUTH2_SERVER_URI 'https://catalog.example.com/oauth/tokens'
);

ATTACH 'warehouse' AS lakehouse (
  TYPE iceberg,
  SECRET iceberg_rest_secret,
  ENDPOINT 'https://catalog.example.com'
);

SELECT *
FROM lakehouse.analytics.orders_iceberg
WHERE order_date >= DATE '2026-01-01';
```

The REST catalog tells DuckDB which snapshot is current and where the table lives; DuckDB still reads Parquet + metadata from your bucket.[cite:140][cite:142]

---

## 8. Design and operations considerations

### 8.1 Single writer (Snowflake) pattern

- In this Option 1 setup, treat **Snowflake as the only writer** to the Iceberg table.
- Other engines (DuckDB, Trino, Spark, etc.) should normally be **read‑only** on this table unless you implement multi‑writer coordination.

### 8.2 Co‑location and egress cost

- Keep Snowflake and your bucket **in the same cloud/region** to avoid extra egress cost and latency.
- Cross‑cloud or cross‑region setups can incur additional data transfer fees.[cite:86][cite:132]

### 8.3 Partitioning and file size

- Choose partitions that work well for Snowflake *and* DuckDB (e.g., daily date partition for event data).
- Aim for reasonably large Parquet files (e.g., 128–512 MB) to balance I/O and parallelism.
- Use Snowflake’s Iceberg tuning options (target file size, compaction) to keep layout healthy.[cite:33][cite:36][cite:159]

### 8.4 Metadata cleanup

- Over time, snapshots and manifests can accumulate.
- Use Iceberg snapshot expiration and compaction (exposed via Snowflake commands/procedures) to clean up old metadata and small files.[cite:31][cite:33][cite:132]

---

## 9. Snowflake table types – deep comparison and why Iceberg is required

This section explains **common Snowflake table types**, why they do not meet the requirement *“DuckDB reads the table by reading files directly from my cloud storage without Snowflake at query time”*, and **why Iceberg on an external volume** is the appropriate choice.

### 9.0 What “suitable” means for this architecture

The target pattern requires **all** of the following at once:

| Requirement | Why it matters |
|---|---|
| **Open files in your bucket** | DuckDB uses `httpfs` + `iceberg` — it needs S3/ADLS/GCS paths and standard Iceberg metadata |
| **Snowflake full DML** | INSERT / UPDATE / DELETE / MERGE with normal table semantics, not read-only external files |
| **ACID table behavior** | Safe concurrent reads while Snowflake writes; consistent snapshots for DuckDB |
| **No Snowflake at DuckDB query time** | Local KPI / analytics runs on DuckDB scanning Parquet + metadata directly |
| **Same logical table, two engines** | One source of truth — not nightly UNLOAD + reload |

Only **Snowflake-managed Iceberg on an external volume** (`CATALOG = 'SNOWFLAKE'`, your bucket) satisfies all five. Every other Snowflake table type fails on at least one.

```text
Target architecture:

  Write path:   ETL / apps  →  Snowflake SQL  →  new Iceberg snapshot  →  files in YOUR bucket
  Read path:    KPI / DuckDB  →  iceberg_scan / REST catalog  →  same bucket (no Snowflake compute)
```

---

### 9.1 Permanent, Transient, and Temporary tables (common Snowflake tables)

These are what most teams mean by **“a normal Snowflake table.”**

#### What they are

| Type | Lifetime | Time Travel | Fail-safe | Typical use |
|---|---|---|---|---|
| **Permanent** | Until dropped | Yes (1–90 days) | Yes | Production analytics tables |
| **Transient** | Until dropped | ~1 day | No | Staging, ETL intermediates |
| **Temporary** | Session only | No | No | Scratch work in a session |

From a **storage and access** perspective, all three behave the same: data lives in **Snowflake-managed storage** in Snowflake’s **micro-partition** format — not as Parquet or Iceberg files you can open elsewhere.[cite:187][cite:116][cite:191][cite:44][cite:171]

#### How Snowflake stores them (why DuckDB is blocked)

```text
What you see in SQL:
  SELECT * FROM analytics.orders;

What exists under the hood:
  Snowflake account
    └─ internal object storage (Snowflake-managed)
         └─ micro-partitions (proprietary, encrypted, optimized for Snowflake)
              └─ metadata in Snowflake’s catalog (NOT Iceberg metadata.json)
```

Key properties:

1. **Proprietary micro-partitions** — columnar, compressed, clustered for Snowflake’s optimizer. Not Parquet, not ORC, not Iceberg data files.
2. **No customer-visible file paths** — you cannot `LIST` or address `s3://...` paths for a permanent table. Snowflake does not expose underlying objects.
3. **Catalog is Snowflake-only** — table definition, statistics, clustering, Time Travel snapshots live in Snowflake internal metadata, not open Iceberg manifests.
4. **Access path is always through Snowflake** — SQL, Snowflake APIs, or **COPY INTO / UNLOAD** to export to open files.

#### Comparison vs your requirement

| Need | Permanent / Transient / Temporary |
|---|---|
| DuckDB reads `s3://bucket/.../metadata/*.json` | **No** — no such paths exist |
| DuckDB reads Parquet data files | **No** — data is micro-partitions |
| Snowflake writes, DuckDB reads same table | **No** — only via export or Snowflake connector |
| Avoid duplicate data copies | **No** — UNLOAD creates a second copy |
| Fresh reads without Snowflake compute | **No** — every path goes through Snowflake or batch export |

#### The UNLOAD workaround (and why it is weak)

```sql
COPY INTO 's3://my-bucket/exports/orders/'
FROM analytics.orders
FILE_FORMAT = (TYPE = PARQUET);
```

Then DuckDB reads the export. Problems:

- **Not the same table** — snapshot at export time, not live
- **No shared ACID** — Snowflake can change `orders` while DuckDB reads a stale export
- **Operational overhead** — schedule, cost, latency, schema drift
- **Double storage** — micro-partitions plus exported Parquet

**Conclusion:** Permanent tables are excellent **inside Snowflake**, but they implement a **warehouse-centric storage model**, not an **open lakehouse file model**.

```text
Permanent table access paths:

  ┌─────────────┐     SQL / API      ┌──────────────┐
  │  Snowflake  │ ◄───────────────── │  Your apps   │
  │ micro-parts │                    └──────────────┘
  └─────────────┘
        ✗ no open s3:// path
        ✗ DuckDB cannot read micro-partitions

  ┌─────────────┐   COPY/UNLOAD      ┌──────────────┐
  │  Snowflake  │ ────────────────► │ Parquet copy │ ◄── DuckDB (stale snapshot)
  └─────────────┘                    └──────────────┘
```

---

### 9.2 External tables

External tables map **your files** (CSV / JSON / Parquet / ORC) in S3 / ADLS / GCS to a Snowflake table definition.[cite:44][cite:189]

```sql
CREATE EXTERNAL TABLE ext_orders (
  order_id STRING,
  amount   NUMBER
)
LOCATION = @my_stage/orders/
FILE_FORMAT = (TYPE = PARQUET);
```

#### What works

| Pros | Detail |
|---|---|
| Data in **your** bucket | DuckDB can read the same Parquet paths |
| Snowflake queries in place | No ingest copy into Snowflake storage |
| Simple lake integration | Good for read-only analytics on raw files |

#### Why external tables are not enough

| Gap | Detail |
|---|---|
| **Read-only in Snowflake** | Snowflake cannot safely UPDATE / DELETE underlying files with full table semantics[cite:44][cite:189] |
| **No table format** | Plain Parquet folders lack Iceberg/Delta transaction semantics |
| **Multi-engine writes are risky** | No ACID guarantee if Snowflake and DuckDB both mutate files |
| **Manual lifecycle** | Compaction, schema evolution, row-level deletes are fragile without a catalog |

External tables answer: **“Snowflake queries my files.”**  
Your requirement needs: **“Snowflake owns a writable table whose storage is open files DuckDB can read safely.”**

```text
External table (plain Parquet):

  s3://bucket/orders/
    part-000.parquet
    part-001.parquet
    part-002.parquet   ← which files are "current" after an UPDATE?

  Snowflake: SELECT only (reads files)
  DuckDB:    can read same Parquet
  Problem:   no atomic snapshot, no safe shared writes
```

**Conclusion:** External tables are great for **read-only lake integration**, but not for a shared, ACID table that Snowflake writes and DuckDB reads as a consistent table.

---

### 9.3 Hybrid tables

Hybrid tables serve Snowflake’s **Unistore** use case (OLTP + analytics in one table type).[cite:44][cite:189][cite:191]

- Optimized for low-latency transactional and hybrid workloads
- Storage is **Snowflake-controlled**, not Iceberg/Parquet in your bucket
- Same access model as permanent tables — no exposed file paths

| Need | Hybrid tables |
|---|---|
| DuckDB direct file read | **No** |
| Open format in your bucket | **No** |
| Full Snowflake DML | **Yes** (inside Snowflake) |

**Conclusion:** Useful for Unistore applications; wrong storage model for DuckDB-off-Snowflake file access.

---

### 9.4 Dynamic, Event, and Directory tables

| Type | Role | Why not for file-level DuckDB |
|---|---|---|
| **Dynamic tables** | Managed incremental views / derived tables from upstream sources[cite:116][cite:189] | Logical layer; underlying storage format unchanged |
| **Event tables** | Telemetry and observability event streams[cite:116] | Snowflake-native; not open bucket layout |
| **Directory tables** | Expose file listings for external stages[cite:116][cite:189] | Metadata only — not a data table |

These are **logical constructs** or metadata tables on top of other storage. None turns underlying data into something DuckDB can read as a standard open table in your bucket.

---

### 9.5 Iceberg on Snowflake-managed storage (`SNOWFLAKE_MANAGED`)

These are still **Iceberg tables**, but data and metadata files sit in **Snowflake’s internal storage**, not in your bucket.[cite:31][cite:88]

| Aspect | External volume (Option 1) | SNOWFLAKE_MANAGED |
|---|---|---|
| Physical location | Your S3 / ADLS / GCS | Snowflake internal storage |
| DuckDB `iceberg_scan('s3://...')` | **Yes** | **No** — no customer-controlled path |
| Snowflake DML | Yes | Yes |
| Open format | Yes (in your bucket) | Yes (but not in your bucket) |

Access often requires Snowflake Open Catalog / Horizon — still Snowflake-governed, not “read my bucket with local credentials.”[cite:88]

**Conclusion:** Good for open format **inside** Snowflake; does not deliver the **“DuckDB reads my bucket without Snowflake”** pattern.

---

### 9.6 Why Iceberg (not just Parquet in a folder)

Plain Parquet in S3:

```text
s3://bucket/orders/data/part-000.parquet
s3://bucket/orders/data/part-001.parquet
```

Open questions without Iceberg:

- Which files are **current**?
- What happens after UPDATE / DELETE (new files + orphaned old files)?
- How do readers avoid **partial writes** mid-commit?
- How is **schema** version tracked?

Iceberg adds a **table layer**:

```text
s3://bucket/orders/
  metadata/
    v1.metadata.json    ← schema, partition spec, snapshot pointer
    snap-123.avro       ← manifest list
  data/
    *.parquet           ← immutable data files
```

| Capability | Plain Parquet folder | Iceberg |
|---|---|---|
| ACID commits | No | Yes (atomic snapshot swap) |
| Time travel / snapshots | Manual | Built-in metadata |
| Schema evolution | Fragile | Versioned in metadata |
| Partition pruning | File listing / guessing | Manifest stats |
| Multi-engine read | Possible but unsafe if anyone writes | Safe with single writer + snapshot isolation |
| Multi-engine write | Risky | Supported with catalog coordination |

---

### 9.7 Master comparison — all table types vs your requirement

| Table type | Storage | Open files in **your** bucket? | Snowflake DML? | DuckDB direct read? | Fit |
|---|---|---|---|---|---|
| **Permanent** | Micro-partitions (Snowflake) | No | Yes | No (only UNLOAD copy) | Poor |
| **Transient** | Same | No | Yes | No | Poor |
| **Temporary** | Same | No | Yes (session) | No | Poor |
| **External** | Your CSV/Parquet | Yes | Read-only | Yes (raw files) | Partial — no ACID writes |
| **Hybrid** | Snowflake-internal | No | Yes (OLTP-style) | No | Poor |
| **Dynamic / Event / Directory** | Depends on source | Usually No | Varies | No | Poor |
| **Iceberg + SNOWFLAKE_MANAGED** | Iceberg in Snowflake storage | No | Yes | Via catalog, not raw bucket | Partial |
| **Iceberg + external volume (Option 1)** | Iceberg Parquet in **your** bucket | **Yes** | **Yes** | **Yes** | **Best fit** |

---

### 9.8 Why Option 1 wins — architecture diagram

```text
Option 1: Snowflake-managed Iceberg + YOUR external volume

  ┌─────────────────────────────────────────────────────────────┐
  │                    Snowflake account                         │
  │  • ICEBERG TABLE catalog (CATALOG = 'SNOWFLAKE')            │
  │  • DML: INSERT / UPDATE / DELETE / MERGE                     │
  │  • Compaction, snapshot expiration, governance               │
  └──────────────────────────┬──────────────────────────────────┘
                             │ writes metadata + Parquet
                             ▼
  ┌─────────────────────────────────────────────────────────────┐
  │     YOUR bucket  s3://my-lakehouse-bucket/tables/orders/     │
  │       metadata/vN.metadata.json  ← DuckDB reads this         │
  │       data/*.parquet             ← DuckDB scans these        │
  └──────────────────────────┬──────────────────────────────────┘
                             │ iceberg_scan / REST catalog
                             ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  DuckDB (local / KPI engine) — NO Snowflake at query time    │
  └─────────────────────────────────────────────────────────────┘
```

**Roles:**

| Party | Role |
|---|---|
| **Snowflake** | Catalog authority, DML, compaction, governance |
| **Your bucket** | Physical home of `metadata/` + `data/` Parquet |
| **DuckDB** | Reads latest snapshot via `iceberg_scan(...)` or REST catalog |

---

### 9.9 Practical implications for KPI / DuckDB workloads

1. **Permanent table as source of truth + DuckDB**  
   Forces Snowflake connector at query time or scheduled UNLOAD — neither is “read files without Snowflake.”

2. **External table as lake**  
   DuckDB can read Parquet, but Snowflake cannot be the sole writer with full MERGE semantics without Iceberg.

3. **Iceberg external volume**  
   Snowflake `INSERT` / `MERGE` → new snapshot → DuckDB `iceberg_scan` sees consistent table state. Matches how DuckDB expects lakehouse tables.

4. **Recommended operational pattern**  
   - **Single writer:** Snowflake (see Section 8.1)  
   - **Readers:** DuckDB; optionally Spark / Trino later  
   - **Co-locate** Snowflake and bucket in the same cloud / region  
   - **Partition spec** that works for both engines (e.g. `order_date`)  
   - Run **compaction and snapshot expiration** on the Snowflake side

---

## 10. Summary — why Option 1 is the appropriate choice

Your requirement:

> “I want to read the files without Snowflake from the cloud location using DuckDB.”

Among Snowflake table types, only **Iceberg tables with external volume storage and Snowflake as catalog** fit cleanly:

- Data + metadata are stored as **standard Iceberg on Parquet** in your bucket.[cite:31][cite:86]
- Snowflake has **full read/write** support and platform features.[cite:31]
- DuckDB can read the same Iceberg table **directly** via `iceberg_scan(path_to_metadata)` or via an Iceberg REST catalog.[cite:140][cite:152][cite:193]

All other table types either:

- Hide storage in Snowflake’s proprietary format (**permanent / transient / temporary / hybrid**).
- Expose raw files without proper writable table semantics (**external tables**).
- Store Iceberg in Snowflake’s own storage instead of your bucket (**Iceberg + SNOWFLAKE_MANAGED**).

Therefore, for a Snowflake-first architecture where DuckDB must operate directly on the same files, **Option 1 – Snowflake-managed Iceberg on an external volume – is the appropriate choice**.
