import argparse
import sys

from rt_trading import v0, v0_acceptance


def main() -> None:
    parser = argparse.ArgumentParser(prog="rt-trading")
    parser.add_argument("command", choices=["v0-check", "v0-acceptance"])
    command = parser.parse_args().command
    sys.exit(v0.check() if command == "v0-check" else v0_acceptance.run())
