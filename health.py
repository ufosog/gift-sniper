"""One command, full diagnostics: python health.py [--online]"""
import sys

from gift_sniper.health import main

if __name__ == "__main__":
    sys.exit(main())
