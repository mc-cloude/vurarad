#!/usr/bin/env python3
"""Export the OpenAPI schema for frontend code generation.

Run: python -m app.tools.export_openapi > openapi.json
This works even when /openapi.json returns 404 in production.
"""

import json
import sys

from app.main import app


def main() -> None:
    schema = app.openapi()
    json.dump(schema, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
