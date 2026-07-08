"""Einmaliger Diagnose-Call: zeigt Status-Code und alle Response-Header von CSFloat.
Dient nur dazu, die echten Rate-Limit-Header-Namen herauszufinden. Kein Teil der finalen App.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("CSFLOAT_API_KEY")

headers = {"Authorization": api_key}


def dump(name, resp):
    print(f"\n--- {name} ---")
    print("Status:", resp.status_code)
    for k, v in resp.headers.items():
        print(f"  {k}: {v}")


resp1 = requests.get("https://csfloat.com/api/v1/me", headers=headers)
dump("GET /api/v1/me", resp1)

resp2 = requests.get(
    "https://csfloat.com/api/v1/listings",
    headers=headers,
    params={"limit": 1},
)
dump("GET /api/v1/listings?limit=1", resp2)
