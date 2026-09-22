FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY preview.py .

# Nothing is persisted: /stream relays the upstream bytes and only the two
# resolutions are cached, in memory. So no VOLUME, deliberately — a preview
# cache on disk would need a size bound nobody would remember to check.
EXPOSE 4792

# `/status` is the liveness answer the hub's /preview/status probe rides on.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('PREVIEW_PORT','4792'); urllib.request.urlopen('http://127.0.0.1:'+p+'/status', timeout=4).read(); sys.exit(0)"

CMD ["python", "preview.py"]
