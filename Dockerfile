FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY monitor.py ./

# Healthcheck verifies the .last_run marker is fresher than 2x CHECK_INTERVAL_HOURS.
HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 \
    CMD test -f /app/.last_run && \
        test $(($(date +%s) - $(stat -c %Y /app/.last_run))) \
            -lt $((${CHECK_INTERVAL_HOURS:-6} * 7200)) \
        || exit 1

CMD ["python", "-u", "monitor.py"]
