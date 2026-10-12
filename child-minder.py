#!/usr/bin/env python3
"""Child Minder daemon entry point.

Python puts this script's real directory first on sys.path, and the
childminder package is installed beside it.
"""

from childminder.daemon import main

if __name__ == "__main__":
    main()
