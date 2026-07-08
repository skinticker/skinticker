"""Prueft nur, ob die .env-Datei korrekt eingelesen wird. Kein API-Call."""

from dotenv import load_dotenv
import os

load_dotenv()


def check(name: str, placeholder: str) -> None:
    value = os.getenv(name)
    if not value or value == placeholder:
        print(f"{name} fehlt oder ist noch der Platzhalter. Bitte .env anpassen.")
    else:
        masked = value[:4] + "..." + value[-4:]
        print(f"{name} gefunden: {masked}")


check("CSFLOAT_API_KEY", "hier_neuen_key_einfuegen")
check("BUFF_API_KEY", "noch_nicht_vorhanden")
