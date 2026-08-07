"""Show a script the way the jobs table will: what each line does, in words.

    .venv/bin/python scripts/describe_script.py scripts/real/20260812_production_100s.txt
    .venv/bin/python scripts/describe_script.py <file> --ev -0.5

The --ev switch applies the exposure compensation the EV box would, so the
speeds printed are the ones the body will actually be set to rather than the
ones written in the file.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from solareclipseworkbench import exposure_trim, job_text


def rows(path):
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # command, moment, sign, offset, then the command's own arguments,
        # with the description last in quotes.
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        command, moment, sign, offset = parts[:4]
        rest = parts[4:]
        description = ""
        if rest and rest[-1].startswith('"'):
            description = rest[-1].strip('"')
            rest = rest[:-1]
        elif rest and rest[-1].endswith('"'):
            # a description containing commas got split; glue it back
            for i, p in enumerate(rest):
                if p.startswith('"'):
                    description = ", ".join(rest[i:]).strip('"')
                    rest = rest[:i]
                    break
        yield command, f"{moment}{sign if sign != '-' else '-'}{offset}", rest, description


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("--ev", type=float, default=0.0,
                    help="exposure compensation, as the EV box applies it")
    args = ap.parse_args()

    if args.ev:
        exposure_trim.set_stops(args.ev)
        print(f"# exposure trim {args.ev:+.1f} EV applied to every frame\n")

    print(f"{'when':28s} {'what':40s} {'description':46s} command")
    print("-" * 130)
    for command, when, fields, description in rows(args.script):
        what = job_text.describe(command, fields)
        clean = job_text.strip_baked_time(description)
        print(f"{when:28s} {what:40s} {clean[:45]:46s} {command}")


if __name__ == "__main__":
    main()
