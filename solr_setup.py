"""(Re)create a Solr collection with our own schema and schemaless mode turned off.

The collection is created with `solr create` inside the Docker container, which works
both in SolrCloud and user-managed mode. Fields come from solr/schema.json (Schema API).

Usage:
    python solr_setup.py                      # collection arxiv
    python solr_setup.py --collection arxiv_p10
"""
import argparse
import json
import subprocess

import requests

from common import ROOT, SOLR, SOLR_CONTAINER


def solr_cli(*args, check=True):
    cmd = ["docker", "exec", SOLR_CONTAINER, "solr", *args]
    print("$", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def post(url, body):
    r = requests.post(url, json=body)
    if not r.ok:
        raise RuntimeError(f"{url} -> {r.status_code}: {r.text[:1000]}")
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", default="arxiv")
    args = ap.parse_args()
    c = args.collection

    solr_cli("delete", "-c", c, check=False)
    out = solr_cli("create", "-c", c)
    print(out.stdout.strip())

    # Turn off schemaless mode: unknown fields are rejected instead of being guessed.
    post(f"{SOLR}/{c}/config", {"set-user-property": {"update.autoCreateFields": "false"}})
    post(f"{SOLR}/{c}/schema", json.loads((ROOT / "solr" / "schema.json").read_text()))

    props = requests.get(f"{SOLR}/{c}/config/overlay").json()["overlay"].get("userProps", {})
    fields = [f["name"] for f in requests.get(f"{SOLR}/{c}/schema/fields").json()["fields"]]
    print("userProps:", props)
    print("fields:", fields)


if __name__ == "__main__":
    main()
