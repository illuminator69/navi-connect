FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hub.py .

# Persisted session/registry lives here — mount a volume at /data.
ENV HUB_STATE=/data/state.json
VOLUME ["/data"]

EXPOSE 4790 4791

# Probe the plain-HTTP health endpoint (python:slim has no curl). Resolves the
# health port the same way hub.py does (HUB_HEALTH_PORT, else HUB_PORT+1).
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('HUB_HEALTH_PORT') or str(int(os.environ.get('HUB_PORT','4790'))+1); urllib.request.urlopen('http://127.0.0.1:'+p+'/', timeout=4).read(); sys.exit(0)"

CMD ["python", "hub.py"]
