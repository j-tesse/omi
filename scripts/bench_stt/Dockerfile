FROM python:3.12-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY bench.py /app/bench.py

ENV HF_HOME=/cache/huggingface
WORKDIR /app
ENTRYPOINT ["python", "-I", "/app/bench.py"]
