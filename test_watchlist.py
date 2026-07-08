"""Testlauf: fragt die guenstigsten Listings fuer alle Watchlist-Items ab."""

import logging
import os

from dotenv import load_dotenv

import config
from csfloat_client import CSFloatClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

load_dotenv()


def main():
    client = CSFloatClient(api_key=os.getenv("CSFLOAT_API_KEY"))

    watchlist = config.load_watchlist()

    for item in watchlist:
        name = item["market_hash_name"]
        listings = client.get_cheapest_listings(name, limit=3)

        if not listings:
            print(f"\n{name}: keine kaufbaren Listings gefunden.")
            continue

        print(f"\n{name}:")
        for listing in listings:
            price_usd_cent = listing["price"]
            float_value = listing["item"]["float_value"]
            print(f"  {price_usd_cent / 100:.2f} $ | Float: {float_value:.4f} | id={listing['id']}")


if __name__ == "__main__":
    main()
