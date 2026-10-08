# Base Debian (OpenSSL 3) : la Connect Box parle un TLS ancien, voir ctx.set_ciphers dans fake_iothub.py.
# Si ton image Docker actuelle utilise une autre base (ligne FROM), reprends la meme ici.
FROM python:3.12-slim

# paho-mqtt DOIT rester en 1.x : fake_iothub.py utilise l'ancienne API de callbacks.
RUN pip install --no-cache-dir "paho-mqtt==1.6.1" cryptography

WORKDIR /app
COPY fake_iothub.py run.py /app/

CMD ["python3", "-u", "/app/run.py"]
