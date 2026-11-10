FROM python:3.12-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r /tmp/requirements.txt

# Bake the cl100k_base BPE file into the image: clients send OpenAI token IDs,
# and decoding them must not need network access at runtime.
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" && chmod -R a+rX /opt/tiktoken

COPY app /srv/app

ENV HF_HOME=/cache/huggingface \
    HOME=/tmp \
    PYTHONUNBUFFERED=1
WORKDIR /srv
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
