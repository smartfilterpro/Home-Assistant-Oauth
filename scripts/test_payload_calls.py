"""Static check: no _build_payload(...) call passes a keyword that is also
supplied by the **common_kwargs it unpacks.

Python raises "got multiple values for keyword argument" for that at call
time, not at import, so it only shows up in Home Assistant's log when the
affected branch runs — which for the active-to-active transition branch was
every heating/cooling status change, silently dropping the segment.

    python scripts/test_payload_calls.py
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Optional argument: check a different copy of the file (used to prove the
# check catches the historical bug).
SRC = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "custom_components" / "smartfilterpro" / "__init__.py"

tree = ast.parse(SRC.read_text(), filename=str(SRC))

# Keys defined by each `<name> = dict(k=..., ...)` assignment, by name.
dict_keys: dict[str, set[str]] = {}
for node in ast.walk(tree):
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "dict"
    ):
        dict_keys[node.targets[0].id] = {kw.arg for kw in node.value.keywords if kw.arg}

failures = 0
calls = 0
for node in ast.walk(tree):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_build_payload"):
        continue
    calls += 1
    explicit = {kw.arg for kw in node.keywords if kw.arg}
    for kw in node.keywords:
        if kw.arg is None and isinstance(kw.value, ast.Name) and kw.value.id in dict_keys:
            clash = explicit & dict_keys[kw.value.id]
            if clash:
                failures += 1
                print(f"  FAIL  line {node.lineno}: _build_payload passes {sorted(clash)} explicitly AND via **{kw.value.id}")

print(f"  checked {calls} _build_payload call(s) against {sorted(dict_keys)}")
if failures == 0:
    print("  PASS  no _build_payload call duplicates a **common_kwargs key")
    print("\nAll checks passed")
    sys.exit(0)
print(f"\n{failures} check(s) failed")
sys.exit(1)
