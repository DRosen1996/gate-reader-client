import argparse
import json
import sys

from .client import Client, ENDPOINTS, HISTORY, ReaderError


def main():
    parser = argparse.ArgumentParser(description="Read-only Gate reader; output is account-sensitive")
    parser.add_argument("endpoint", choices=sorted(ENDPOINTS))
    parser.add_argument("--contract")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int)
    parser.add_argument("--from", dest="start", type=int)
    parser.add_argument("--to", dest="end", type=int)
    parser.add_argument("--holding", choices=("true", "false"))
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--history", action="store_true", help="Read all pages/windows of explicit historical range")
    args = parser.parse_args()
    params = {k: v for k, v in {"contract": args.contract, "limit": args.limit,
        "offset": args.offset, "from": args.start, "to": args.end}.items() if v is not None}
    if args.holding is not None:
        params["holding"] = args.holding == "true"
    try:
        client = Client(args.timeout)
        if args.history:
            if args.endpoint not in HISTORY or args.start is None or args.end is None:
                raise ReaderError("--history requires historical endpoint and --from/--to")
            del params["from"], params["to"]
            for record in client.history(args.endpoint, args.start, args.end, include_context=True, **params):
                print(json.dumps(record, ensure_ascii=False))
        else:
            print(json.dumps(client.query(args.endpoint, **params), ensure_ascii=False))
    except ReaderError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
