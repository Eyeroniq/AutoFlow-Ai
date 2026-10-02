"""Print the API's OpenAPI document as JSON, for generating packages/shared's TypeScript types.

    docker compose exec -T api python -m app.openapi_export > packages/shared/openapi.json

(scripts/generate-shared-types.sh does both steps.) The output is stable: keys sorted, no timestamps.
"""

import json

from app.main import app


def main() -> None:
    print(json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
