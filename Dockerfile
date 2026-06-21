FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y gcc && apt-get autoremove -y

COPY db ./db
COPY handlers ./handlers
COPY utils ./utils
COPY scripts ./scripts
COPY main.py .

ENTRYPOINT ["python", "/app/main.py"]
