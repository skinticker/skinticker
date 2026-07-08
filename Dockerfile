FROM python:3.14-slim

# Python-Ausgabe sofort schreiben (nicht puffern), damit Logs im Container Manager erscheinen.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY dashboard/ dashboard/

CMD ["python", "main.py"]
