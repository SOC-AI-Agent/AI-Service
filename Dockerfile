FROM python:3.12-slim

WORKDIR /app

# CPU-only torch, before anything that depends on it. The default wheel from
# PyPI brings CUDA libraries worth gigabytes that this service never uses.
RUN pip install --no-cache-dir torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt requirements-embed.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-embed.txt

# The embedding model, baked into the image. At runtime the hub is switched off
# (HF_HUB_OFFLINE), so the service never reaches the internet for weights, and a
# host that cannot reach huggingface.co still starts with the model it was built
# with. Building does need that access. EMBED_MODEL in .env must name the same
# model, or matching by meaning reports it missing and stays off.
ARG EMBED_MODEL=BAAI/bge-small-en-v1.5
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBED_MODEL}', cache_folder='/models', device='cpu')"
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    EMBED_CACHE_DIR=/models

COPY . .

# /data holds the playbook index. Created here and owned by the service user, so
# the named volume mounted over it starts out writable.
RUN useradd --create-home --uid 10001 soc \
    && mkdir -p /data \
    && chown -R soc:soc /app /data /models
USER soc

EXPOSE 8100

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
