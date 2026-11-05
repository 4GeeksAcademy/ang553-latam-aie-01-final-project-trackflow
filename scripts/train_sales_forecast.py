"""Train and evaluate TrackFlow's leakage-safe monthly sales forecast."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from pipelines.sales_forecast import run_experiment  # noqa: E402


def main() -> int:
    result = run_experiment()
    metadata = result["metadata"]
    summary = {
        "status": "completed",
        "predictions": str(result["predictions_path"]),
        "metrics": str(result["metrics_path"]),
        "plot": str(result["plot_path"]),
        "random_forest": metadata["metrics"]["random_forest"],
        "seasonal_baseline": metadata["metrics"]["seasonal_baseline"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
