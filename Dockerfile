FROM ghcr.io/n8n-io/n8n:2.9.4

USER root

# Instala o Python e o pip
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Copia os scripts Python e o requirements para a pasta do n8n
COPY *.py /home/node/
COPY requirements.txt /home/node/

# Instala as bibliotecas Python
RUN pip3 install --no-cache-dir --break-system-packages -r /home/node/requirements.txt

USER node
