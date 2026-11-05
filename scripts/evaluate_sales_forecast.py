"""Run TrackFlow's temporal evaluation of the inherited sales forecast."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from pipelines.eval.regression_evaluation import run_evaluation  # noqa: E402


def main() -> int:
    result = run_evaluation()
    metadata = result["metadata"]
    print(
        json.dumps(
            {
                "status": "completed",
                "metrics": str(result["metrics_path"]),
                "plot": str(result["plot_path"]),
                "report": str(result["report_path"]),
                "diagnosis": metadata["diagnosis"]["category"],
                "validation_mae": metadata["summary"]["validation_mae_eur"]["formatted"],
                "baseline_mae": metadata["summary"]["baseline_mae_eur"]["formatted"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
