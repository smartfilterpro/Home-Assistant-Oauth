"""Checks the environment table and the helpers around it.

const.py has no Home Assistant imports, so this runs with plain Python:

    python scripts/test_environments.py
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "smartfilterpro"

spec = importlib.util.spec_from_file_location("sfp_const", COMPONENT / "const.py")
const = importlib.util.module_from_spec(spec)
spec.loader.exec_module(const)

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"  PASS  {name}")
    else:
        failures += 1
        print(f"  FAIL  {name}{(': ' + detail) if detail else ''}")


envs = const.ENVIRONMENTS

check("production and dev environments exist", {"production", "dev"} <= set(envs))
check("every environment has a Bubble base and a Core URL",
      all({"api_base", "core_ingest_url"} <= set(e) for e in envs.values()))
check("every environment uses https", all(
    e["api_base"].startswith("https://") and e["core_ingest_url"].startswith("https://") for e in envs.values()))
check("production Bubble is the live app, dev is version-test",
      envs["production"]["api_base"] == "https://smartfilterpro.com"
      and envs["dev"]["api_base"].endswith("/version-test"))
check("production and dev post to DIFFERENT Core services",
      envs["production"]["core_ingest_url"] != envs["dev"]["core_ingest_url"])
check("every Core URL is the batch ingest endpoint",
      all(e["core_ingest_url"].endswith("/ingest/v1/events:batch") for e in envs.values()))
check("default environment is production", const.DEFAULT_ENVIRONMENT == "production")
check("CORE_INGEST_URL (legacy name) is production's Core",
      const.CORE_INGEST_URL == envs["production"]["core_ingest_url"])

# Entries created before the selector only stored api_base.
check("old entry with the live Bubble base maps to production",
      const.environment_for_api_base("https://smartfilterpro.com") == "production")
check("old entry with the version-test base maps to dev (and so to dev Core)",
      const.environment_for_api_base("https://smartfilterpro.com/version-test/") == "dev"
      and const.core_ingest_url_for(None, "https://smartfilterpro.com/version-test") == envs["dev"]["core_ingest_url"])
check("empty or unknown base falls back to production",
      const.environment_for_api_base("") == "production"
      and const.environment_for_api_base("https://example.com") == "production")
check("an explicit environment wins over api_base",
      const.core_ingest_url_for("dev", "https://smartfilterpro.com") == envs["dev"]["core_ingest_url"])
check("an unknown explicit environment falls back to api_base",
      const.core_ingest_url_for("staging", "https://smartfilterpro.com/version-test") == envs["dev"]["core_ingest_url"])

manifest = json.loads((COMPONENT / "manifest.json").read_text())
hacs = json.loads((ROOT / "hacs.json").read_text())
check("manifest version is semantic", __import__("re").fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", manifest["version"]) is not None)
check("manifest issue tracker points at this repo", "smartfilterpro/Home-Assistant-Oauth" in manifest["issue_tracker"])
check("hacs.json hides the default branch so only releases are installable", hacs.get("hide_default_branch") is True)
check("hacs.json declares a minimum Home Assistant version", bool(hacs.get("homeassistant")))

translations = json.loads((COMPONENT / "translations" / "en.json").read_text())
check("login form has a label for the environment field",
      "environment" in translations["config"]["step"]["user"]["data"])

print("\nAll checks passed" if failures == 0 else f"\n{failures} check(s) failed")
sys.exit(0 if failures == 0 else 1)
