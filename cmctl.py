#!/usr/bin/env python3
"""Child Minder management utility (cmctl) entry point.

Python puts this script's real directory first on sys.path, so the
childminder package beside it is found through the /usr/bin symlinks too.
"""

from childminder.cli import main

if __name__ == "__main__":
    main()
