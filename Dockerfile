FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hub.py .

# Persisted session/registry lives here — mount a volume at /data.
ENV HUB_STATE=/data/state.json
VOLUME ["/data"]

EXPOSE 4790
CMD ["python", "hub.py"]
