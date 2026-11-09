import argparse
import json
import sys
from pathlib import Path


def _verify(path: str, expect: str = None) -> int:
    from .record import verify_record
    try:
        bundle = json.loads(Path(path).read_text(encoding="utf-8"))
        problems = verify_record(bundle, expected_envelope_digest=expect)
    except (OSError, ValueError, AttributeError, KeyError, TypeError) as e:
        problems = [f"UNREADABLE_BUNDLE {type(e).__name__}: {e}"]
    if not problems:
        rows = len(bundle.get("rows", []))
        print(f"PASS {path}: {rows} rows match the stored digest and recorded fields")
        return 0
    print(f"FAIL {path}")
    for p in problems:
        print(f"  {p}")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rate_locked", description="Rate-Locked Approval (sandbox only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="offline end-to-end story on the local fake; writes the plan card prototype")
    d.add_argument("--out", default="prototype", help="output folder (default: prototype)")
    f = sub.add_parser("fake-server", help="run the local Airwallex fake on loopback")
    f.add_argument("--port", type=int, default=8765)
    s = sub.add_parser("serve", help="web console on http://127.0.0.1 (fake world: no keys, no network; sandbox world: needs keys, see docs/SANDBOX_RUN.md)")
    s.add_argument("--world", choices=("fake", "sandbox"), default="fake")
    s.add_argument("--port", type=int, default=8770)
    s.add_argument("--stage", choices=("video",), default=None, help="larger type for recording")
    s.add_argument("--reader", choices=("scripted", "model"), default="scripted",
                   help="invoice reader: the scripted TEST DOUBLE (default, no model) or a live model (needs the SDK and a key)")
    v = sub.add_parser("verify", help="check an exported record copy offline")
    v.add_argument("bundle")
    v.add_argument("--expect-digest", default=None, help="the envelope digest the approver saw")
    r = sub.add_parser("reader-check", help="read the three fictional invoices with the live model reader and report (needs the SDK and a key)")
    r.add_argument("--model", default=None, help="model id (default: RATE_LOCKED_MODEL or the built-in default)")
    r.add_argument("--out", default="evidence", help="folder for the evidence file")
    c = sub.add_parser("check-shapes", help="compare a sandbox probe evidence file with the wire parsers")
    c.add_argument("evidence")
    args = ap.parse_args(argv)
    if args.cmd == "demo":
        from .demo import main as demo_main
        demo_main(Path(args.out))
        return 0
    if args.cmd == "serve":
        from .server import main as serve_main
        return serve_main(world=args.world, port=args.port, stage=args.stage, reader=args.reader)
    if args.cmd == "reader-check":
        from . import model_reader
        try:
            reader = model_reader.make_reader(model=args.model)
        except model_reader.ReaderSetupError as e:
            print(f"Cannot start the reader check: {e}")
            return 2
        return model_reader.run_reader_check(reader, out_dir=args.out)
    if args.cmd == "check-shapes":
        from .shapecheck import main as shapes_main
        return shapes_main(args.evidence)
    if args.cmd == "verify":
        return _verify(args.bundle, args.expect_digest)
    from .awx.fake_server import main as fake_main
    sys.argv = ["fake_server", "--port", str(args.port)]
    fake_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
