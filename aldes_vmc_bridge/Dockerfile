# Debian base image (OpenSSL 3): The Connect Box uses an older version of TLS; see `ctx.set_ciphers` in `fake_iothub.py`.
# If your current Docker image uses a different base image (the `FROM` line), use the same one here.
FROM python:3.12-slim

# paho-mqtt MUST remain at version 1.x: fake_iothub.py uses the old callback API.
RUN pip install --no-cache-dir "paho-mqtt==1.6.1" cryptography

WORKDIR /app
COPY fake_iothub.py run.py /app/

CMD ["python3", "-u", "/app/run.py"]
