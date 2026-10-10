"""Read-only FYERS session and symbol diagnostic. Never places an order."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
from fyers_market_data import FyersMarketDataClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("right", nargs="?", choices=("CE", "PE"))
    parser.add_argument("strike", nargs="?", type=float)
    parser.add_argument("expiry", nargs="?", help="Expiry YYYY-MM-DD")
    args = parser.parse_args()
    try:
        client = FyersMarketDataClient()
        client.validate_session()
        print("FYERS profile OK")
        if args.right and args.strike is not None and args.expiry:
            from datetime import date
            symbol = client.symbols.option_symbol("NIFTY", date.fromisoformat(args.expiry), args.strike, args.right)
            print(f"Resolved FYERS symbol: {symbol}")
        else:
            print("Pass CE|PE STRIKE YYYY-MM-DD to verify an option symbol.")
        print("Read-only diagnostic: no orders were placed.")
        return 0
    except Exception as exc:
        print(f"FYERS diagnostic failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
