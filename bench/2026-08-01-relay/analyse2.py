"""Match card frames to commanded actions, once the body's clock is aligned."""

import csv
import json
from datetime import datetime, timezone

CARD = "card.csv"
LOG = "campaign_relay_1785601757.json"


def parse_exposure(text):
    if not text or text.startswith("-"):
        return None
    if "/" in text:
        num, den = text.split("/")
        return float(num) / float(den)
    return float(text)


rows = []
with open(CARD) as handle:
    for row in csv.DictReader(handle):
        stamp = row["DateTimeOriginal"]
        if not stamp or stamp.startswith("-"):
            continue
        naive = datetime.strptime(stamp, "%Y:%m:%d %H:%M:%S")
        rows.append({
            "file": row["FileName"],
            "utc": naive.replace(tzinfo=timezone.utc).timestamp(),
            "exp": parse_exposure(row["ExposureTime"]),
            "exp_text": row["ExposureTime"],
            "iso": row["ISO"],
            "type": row["ShutterType"],
            "drive": row["DriveMode"],
            "seq": row["SequenceNumber"],
            "bkt": row["AutoBracketing"],
            "count": int(row["ImageCount"]),
        })
rows.sort(key=lambda r: r["count"])

log = json.load(open(LOG))
actions = [r for r in log["records"] if r["action"] != "quiet"]


def windows(shift):
    """Frames per action for a given clock shift, plus how many land nowhere."""
    hits, claimed = [], set()
    for record in actions:
        start = record.get("s2_closed_at", record["wall_start"]) - 1.0
        end = record["wall_end"] + 2.5
        inside = [i for i, r in enumerate(rows) if start <= r["utc"] + shift <= end]
        hits.append(inside)
        claimed.update(inside)
    return hits, len(claimed)


best_shift, best_claimed = 0, -1
for shift in range(-7200, 7201):
    _, claimed = windows(shift)
    if claimed > best_claimed:
        best_claimed, best_shift = claimed, shift

hits, claimed = windows(best_shift)
print(f"clock shift {best_shift:+d} s   frames attributed {claimed}/{len(rows)}")
for row in rows:
    row["t"] = row["utc"] + best_shift

print(f"\nactuations {rows[0]['count']}..{rows[-1]['count']}, "
      f"{len(rows)} files, gaps: "
      f"{(rows[-1]['count'] - rows[0]['count'] + 1) - len(rows)}")

print("\n--- commanded action -> frames landed ---")
print(f"  {'block':<14} {'action':<20} {'held':>6} {'frames':>7} {'rate':>10}  exposures")
for record, inside in zip(actions, hits):
    block = record["block"].split(":")[0]
    hold = record.get("hold_s")
    frames = [rows[i] for i in inside]
    rate = ""
    if hold and len(frames) > 1:
        rate = f"{len(frames) / hold:.2f} fps"
    exposures = []
    for frame in frames:
        if not exposures or exposures[-1] != frame["exp_text"]:
            exposures.append(frame["exp_text"])
    shown = ",".join(exposures[:9]) + ("..." if len(exposures) > 9 else "")
    print(f"  {block:<14} {record['action']:<20} {hold if hold else '-':>6} "
          f"{len(frames):>7} {rate:>10}  {shown}")

print("\n--- bracket sequences, frame by frame ---")
for record, inside in zip(actions, hits):
    if not record["action"].startswith("bkt"):
        continue
    frames = [rows[i] for i in inside]
    print(f"\n  {record['action']}  ({len(frames)} frames)")
    previous = None
    for frame in frames:
        gap = f"{frame['t'] - previous:+5.1f}s" if previous else "  ref"
        print(f"    {frame['file']}  seq={frame['seq']:>2}  {frame['exp_text']:>8}  {gap}")
        previous = frame["t"]

print("\n--- burst rate decay within the long holds ---")
for record, inside in zip(actions, hits):
    if not record["action"].startswith("burst"):
        continue
    frames = [rows[i] for i in inside]
    if len(frames) < 4:
        continue
    base = frames[0]["t"]
    buckets = {}
    for frame in frames:
        buckets.setdefault(int(frame["t"] - base), 0)
        buckets[int(frame["t"] - base)] += 1
    trace = " ".join(f"{buckets.get(s, 0)}" for s in range(int(record["hold_s"]) + 2))
    print(f"  {record['action']:<20} {record['exp_text'] if 'exp_text' in record else '':>0}"
          f"frames/second: {trace}")
