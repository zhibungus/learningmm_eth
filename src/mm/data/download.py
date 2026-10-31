"""Historical downloader for data.binance.vision (USD-M futures) -> data/raw + data/processed.

For every (dataset, period) it:
  1. downloads the zip and its ``.CHECKSUM`` into ``data/raw/binance_um/<dataset>/<SYMBOL>/``
     (originals are never modified),
  2. verifies SHA-256 against the checksum file (a mismatch deletes the file and raises),
  3. normalizes the CSV inside to the canonical schema and writes
     ``data/processed/binance_um/<processed_name>/<SYMBOL>/<period>/part-0.parquet``.

Availability on the archive has gaps (404). Missing files are logged and skipped, never faked.
Known (as of 2026-10): ETHUSDT ``bookTicker`` only exists for 2023-05-16 .. 2024-03-30.
``fundingRate`` is only published monthly, and only after the month has ended; for a month that
is not in the archive yet we fall back to the REST API (which also has the mark price).

Examples (run from the project root, with ``.venv`` active and ``pip install -e .`` done)::

    python -m mm.data.download --start 2026-10-16 --end 2026-10-17
    python -m mm.data.download --datasets bookTicker --start 2024-03-30 --end 2024-03-30
    python -m mm.data.download --datasets aggTrades trades --symbol ETHUSDT --start 2026-10-01 --end 2026-10-07
    python -m mm.data.download --list-availability --datasets bookTicker
"""
# Postpones type-hint evaluation so hints like `str | None` are always safe.
from __future__ import annotations

import argparse  # command-line interface
import datetime as dt
import hashlib  # SHA-256 checksums to prove a download is intact
import json
import sys
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET  # parses the XML bucket listing returned by S3
from pathlib import Path

import polars as pl
import requests

from . import normalize as N
from .config import load_config, processed_dir, raw_dir

# Dispatch table: a dict mapping dataset name -> the function that normalizes its CSV. Storing
# functions in a dict (they are ordinary values) avoids a long if/elif chain.
_CSV_PARSERS = {
    "aggTrades": N.agg_trades_csv_to_frame,
    "trades": N.trades_csv_to_frame,
    "bookTicker": N.book_ticker_csv_to_frame,
    "fundingRate": N.funding_csv_to_frame,
}


# ---------------------------------------------------------------- helpers
def parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def periods(start: dt.date, end: dt.date, granularity: str) -> list[str]:
    """Period labels covering [start, end]: 'YYYY-MM-DD' (daily) or 'YYYY-MM' (monthly)."""
    if end < start:
        raise ValueError("end before start")
    # Daily files: one label per calendar day, built with a list comprehension.
    if granularity == "daily":
        n = (end - start).days + 1
        return [(start + dt.timedelta(days=i)).isoformat() for i in range(n)]
    # Monthly files: step month by month. Tuples compare element-wise, so (y, m) <= (y2, m2)
    # means "this month is not after that month". December rolls over to January of next year.
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def archive_url(cfg: dict, dataset: str, symbol: str, period: str) -> str:
    # URL pattern on data.binance.vision, e.g. .../daily/aggTrades/ETHUSDT/ETHUSDT-aggTrades-2026-10-17.zip
    a = cfg["archive"]
    gran = a["datasets"][dataset]["granularity"]
    return (f"{a['base_url']}/{a['market_path']}/{gran}/{dataset}/{symbol}/"
            f"{symbol}-{dataset}-{period}.zip")


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    # Hash the file in 1 MiB blocks so even a multi-GB file never has to fit in memory.
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        # `:=` (the walrus operator) assigns AND tests in one expression; the loop ends at EOF,
        # when read() returns empty bytes (falsy).
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def _http_get_to_file(url: str, dest: Path, cfg: dict) -> int:
    """Stream ``url`` to ``dest``. Returns the HTTP status (404 is returned, not raised)."""
    a = cfg["archive"]
    # Retry transient network errors with growing waits (1s, 2s, 4s). A 404 is NOT an error:
    # it just means the file does not exist for that date, which the caller must handle.
    last_exc: Exception | None = None
    for attempt in range(a["retries"]):
        try:
            # stream=True downloads in pieces instead of loading the whole zip into memory; the
            # `with` block closes the connection afterwards.
            with requests.get(url, stream=True, timeout=a["timeout_s"]) as r:
                if r.status_code == 404:
                    return 404
                r.raise_for_status()
                # Write to a .part file and rename when complete, so an interrupted download is
                # never mistaken for a finished one.
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(a["chunk_bytes"]):
                        fh.write(chunk)
                tmp.replace(dest)
                return r.status_code
        except requests.RequestException as exc:  # network hiccup: retry
            last_exc = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"download failed after retries: {url}: {last_exc}")


def verify_checksum(zip_path: Path, checksum_path: Path) -> bool:
    """True if SHA-256 of the zip equals the hash in the ``.CHECKSUM`` file."""
    # The .CHECKSUM file looks like: "<sha256hex>  <filename>"; the hash is the first word.
    expected = checksum_path.read_text().split()[0].lower()
    return sha256_file(zip_path) == expected


def list_available(cfg: dict, dataset: str, symbol: str) -> list[str]:
    """Period labels that really exist on the archive (S3 bucket listing)."""
    a = cfg["archive"]
    gran = a["datasets"][dataset]["granularity"]
    # Ask the S3 bucket behind data.binance.vision which files really exist (the site has
    # availability gaps, so we check instead of guessing). Listings are paginated (<=1000 keys
    # per page), hence the loop with `marker`.
    prefix = f"{a['market_path']}/{gran}/{dataset}/{symbol}/"
    base = a["base_url"].replace("https://data.binance.vision", "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision")
    keys, marker = [], ""
    while True:
        r = requests.get(base, params={"prefix": prefix, "marker": marker}, timeout=a["timeout_s"])
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {"s": root.tag.split("}")[0].strip("{")}
        page = [e.text for e in root.findall("s:Contents/s:Key", ns)]
        keys += page
        if root.findtext("s:IsTruncated", namespaces=ns) != "true" or not page:
            break
        marker = page[-1]
    # Strip the file-name prefix and ".zip" to leave just the period label ('2024-03-30').
    stem = f"{symbol}-{dataset}-"
    return sorted(k.rsplit("/", 1)[1][len(stem):-4] for k in keys if k.endswith(".zip"))


def _compress_ranges(labels: list[str]) -> str:
    """'2024-03-28','2024-03-29','2024-03-30' -> '2024-03-28..2024-03-30' (daily labels only)."""
    if not labels or len(labels[0]) != 10:
        return ", ".join(labels)
    # Walk the sorted dates, extending the current run while each date is exactly 1 day after
    # the previous; `None` appended at the end flushes the last run.
    days = [dt.date.fromisoformat(x) for x in labels]
    out, s, p = [], days[0], days[0]
    for d in days[1:] + [None]:
        if d is not None and (d - p).days == 1:
            p = d
            continue
        out.append(f"{s}..{p}" if s != p else f"{s}")
        s = p = d
    return ", ".join(out)


# ---------------------------------------------------------------- REST funding
def fetch_funding_rest(cfg: dict, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """GET /fapi/v1/fundingRate pages between two ms timestamps."""
    # The REST endpoint returns at most `limit` rows per call, so page forward in time using the
    # last returned fundingTime + 1 ms as the next startTime.
    r_cfg = cfg["rest"]
    rows: list[dict] = []
    cur = start_ms
    while cur < end_ms:
        resp = requests.get(r_cfg["base_url"] + r_cfg["funding_path"], timeout=r_cfg["timeout_s"],
                            params={"symbol": symbol, "startTime": cur, "endTime": end_ms,
                                    "limit": r_cfg["funding_limit"]})
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        rows += page
        cur = int(page[-1]["fundingTime"]) + 1
        if len(page) < r_cfg["funding_limit"]:
            break
    return rows


def _month_bounds_ms(label: str) -> tuple[int, int]:
    # 'YYYY-MM' -> (first ms of that month, last ms of that month) in UTC. [:4] and [5:7] are
    # string slices (characters 0-3 and 5-6).
    y, m = int(label[:4]), int(label[5:7])
    s = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
    e = dt.datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=dt.timezone.utc)
    return int(s.timestamp() * 1000), int(e.timestamp() * 1000) - 1


# ---------------------------------------------------------------- one period
def _out_path(cfg: dict, dataset: str, symbol: str, period: str) -> Path:
    # data/processed/binance_um/<processed name>/<SYMBOL>/<period>/part-0.parquet
    name = cfg["archive"]["datasets"][dataset]["processed"]
    return processed_dir(cfg) / name / symbol / period / "part-0.parquet"


def _write_processed(df: pl.DataFrame, out: Path) -> None:
    """Write one parquet file (zstd-compressed), creating its folder if needed."""
    out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out, compression="zstd")


def process_zip(cfg: dict, dataset: str, symbol: str, period: str, zip_path: Path,
                add_marks: bool = False) -> int:
    """Normalize one verified zip to parquet. Returns the row count."""
    # Unzip to a throw-away temporary folder. We copy the single CSV to our OWN file name instead
    # of using the name stored inside the zip (downloaded files are untrusted; a crafted name
    # could try to write outside the folder).
    with zipfile.ZipFile(zip_path) as z:
        members = [n for n in z.namelist() if n.endswith(".csv")]
        if len(members) != 1:
            raise RuntimeError(f"{zip_path.name}: expected 1 csv, found {members}")
        with tempfile.TemporaryDirectory(prefix="mm_unzip_") as tmp:
            csv_path = Path(tmp) / "data.csv"  # our own name, never the name from the zip
            with z.open(members[0]) as src, open(csv_path, "wb") as dst:
                while block := src.read(1 << 20):
                    dst.write(block)
            # Look up the right parser, run it (it returns a LAZY query), and .collect() executes it.
            df = _CSV_PARSERS[dataset](csv_path).collect()
    if dataset == "fundingRate" and add_marks:
        df = _add_marks(cfg, symbol, period, df)
    _write_processed(df, _out_path(cfg, dataset, symbol, period))
    return df.height


def _add_marks(cfg: dict, symbol: str, period: str, df: pl.DataFrame) -> pl.DataFrame:
    # The archive funding CSV has no mark price. Fetch it from REST and attach it to each row by
    # nearest timestamp (join_asof) within 60 s, since the two sources differ by a few ms.
    s, e = _month_bounds_ms(period)
    rest = N.funding_rest_to_frame(fetch_funding_rest(cfg, symbol, s, e))
    if rest.is_empty():
        return df
    tol = 60 * 1_000_000_000
    marks = rest.select("ts_ns", pl.col("mark_price").alias("mp"))
    return (df.drop("mark_price").sort("ts_ns")
            .join_asof(marks, on="ts_ns", strategy="nearest", tolerance=tol)
            .rename({"mp": "mark_price"}).select(df.columns))


def fetch_period(cfg: dict, dataset: str, symbol: str, period: str,
                 add_marks: bool = False, force: bool = False) -> dict:
    """Download + verify + normalize one period. Returns a status dict (also logged)."""
    # Steps: download zip + checksum -> verify -> normalize to parquet. Cached files are re-verified
    # rather than re-downloaded, so reruns are cheap and safe.
    gran = cfg["archive"]["datasets"][dataset]["granularity"]
    dest_dir = raw_dir(cfg) / dataset / symbol
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / f"{symbol}-{dataset}-{period}.zip"
    ck_path = zip_path.with_name(zip_path.name + ".CHECKSUM")
    url = archive_url(cfg, dataset, symbol, period)
    status = {"dataset": dataset, "symbol": symbol, "period": period, "url": url}

    if not (zip_path.exists() and ck_path.exists()) or force:
        if _http_get_to_file(url, zip_path, cfg) == 404:
            if dataset == "fundingRate" and gran == "monthly":
                return _funding_from_rest(cfg, symbol, period, dest_dir, status)
            status["status"] = "missing_on_archive"
            return status
        if _http_get_to_file(url + ".CHECKSUM", ck_path, cfg) == 404:
            zip_path.unlink(missing_ok=True)
            status["status"] = "missing_checksum"
            return status
    if not verify_checksum(zip_path, ck_path):
        zip_path.unlink(missing_ok=True)
        ck_path.unlink(missing_ok=True)
        raise RuntimeError(f"CHECKSUM MISMATCH for {zip_path.name}; file deleted")
    status["sha256"] = sha256_file(zip_path)
    status["bytes"] = zip_path.stat().st_size
    status["rows"] = process_zip(cfg, dataset, symbol, period, zip_path, add_marks)
    status["status"] = "ok"
    return status


def _funding_from_rest(cfg: dict, symbol: str, period: str, dest_dir: Path, status: dict) -> dict:
    s, e = _month_bounds_ms(period)
    rows = fetch_funding_rest(cfg, symbol, s, e)
    if not rows:
        status["status"] = "missing_on_archive_and_rest"
        return status
    (dest_dir / f"{symbol}-fundingRate-{period}.rest.json").write_text(json.dumps(rows))
    df = N.funding_rest_to_frame(rows)
    _write_processed(df, _out_path(cfg, "fundingRate", symbol, period))
    status.update(status="ok_from_rest", rows=df.height)
    return status


# ---------------------------------------------------------------- CLI
# `argv=None` means "use the real command line"; tests can pass a list instead.
def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbol", default=cfg["symbol"])
    ap.add_argument("--start", type=parse_date, help="first date YYYY-MM-DD (UTC)")
    ap.add_argument("--end", type=parse_date, help="last date YYYY-MM-DD, inclusive")
    ap.add_argument("--datasets", nargs="+", default=cfg["archive"]["default_datasets"],
                    choices=sorted(cfg["archive"]["datasets"]))
    ap.add_argument("--funding-marks", action="store_true",
                    help="fill funding mark_price from the REST API (archive CSV lacks it)")
    ap.add_argument("--force", action="store_true", help="re-download even if present")
    ap.add_argument("--list-availability", action="store_true",
                    help="only print which periods exist on the archive, then exit")
    args = ap.parse_args(argv)

    if args.list_availability:
        for ds in args.datasets:
            av = list_available(cfg, ds, args.symbol)
            print(f"{ds}: {len(av)} files; {_compress_ranges(av) if av else 'NONE'}")
        return 0
    if not (args.start and args.end):
        ap.error("--start and --end are required")

    # Every attempt is appended to a JSON-lines log (one JSON object per line) with the sha256,
    # so we keep an audit trail of exactly what was downloaded.
    log_path = raw_dir(cfg) / "download_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rc = 0
    for ds in args.datasets:
        gran = cfg["archive"]["datasets"][ds]["granularity"]
        for p in periods(args.start, args.end, gran):
            try:
                st = fetch_period(cfg, ds, args.symbol, p, args.funding_marks, args.force)
            # Catch per-file failures so one bad date doesn't abort the whole range.
            except Exception as exc:  # report and keep going; non-zero exit at the end
                st = {"dataset": ds, "period": p, "status": "error", "error": str(exc)}
                rc = 1
            st["logged_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(st) + "\n")
            print(f"{ds:12s} {p:10s} {st['status']:28s} rows={st.get('rows', '-')}")
    return rc


# Run main() only when executed as a script, and pass its return value as the exit code.
if __name__ == "__main__":
    sys.exit(main())
