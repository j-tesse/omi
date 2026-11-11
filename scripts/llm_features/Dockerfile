FROM python:3.12-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Harness code is bind-mounted at /app at run time (scripts/test_llm_features.sh),
# the upstream submodule read-only at /upstream.
WORKDIR /app
ENTRYPOINT ["python", "/app/run.py"]
