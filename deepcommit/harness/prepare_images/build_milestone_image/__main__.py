"""Allow running the module directly with python -m."""

import sys

from .build_milestone_image import main

if __name__ == "__main__":
    sys.exit(main())
