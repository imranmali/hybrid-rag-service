"""Check that the pipelines in load/query.js equal the eval configs they were copied from.

Usage:
    k6 inspect load/query.js > inspect.json
    python load/check_pipelines.py inspect.json [evals/configs]

Retrieval and rerank scenarios must equal grid rows 4 and 5 except generation.enabled == false;
the full scenario must equal grid row 6 exactly (load-testing spec, pipelines match configs).
"""

import copy
import json
import sys
from pathlib import Path

import yaml

SCENARIOS = {  # scenario -> (config file stem, generation.enabled override or None)
    "retrieval": ("04-contextual", False),
    "rerank": ("05-rerank", False),
    "full": ("06-routing", None),
}


def main() -> int:
    inspect_path = Path(sys.argv[1])
    configs = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("evals/configs")
    in_query = json.loads(inspect_path.read_text())["ext"]["hybridrag"]["pipelines"]
    failures = 0
    for scenario, (stem, generation) in SCENARIOS.items():
        expected = copy.deepcopy(yaml.safe_load((configs / f"{stem}.yaml").read_text())["pipeline"])
        if generation is not None:
            expected["generation"]["enabled"] = generation
        if in_query[scenario] == expected:
            print(f"ok   {scenario}: equals {stem}.yaml pipeline (generation: {generation})")
        else:
            failures += 1
            print(f"FAIL {scenario}: differs from {stem}.yaml")
            print("  load/query.js:", json.dumps(in_query[scenario], sort_keys=True))
            print("  expected     :", json.dumps(expected, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
