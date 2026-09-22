FROM python:3.12-slim

WORKDIR /app

# No build toolchain and no libpq: nothing here compiles a wheel or speaks the
# Postgres wire protocol any more.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 soc && chown -R soc:soc /app
USER soc

EXPOSE 8100

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
