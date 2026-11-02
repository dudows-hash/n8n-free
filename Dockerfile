FROM ghcr.io/n8n-io/n8n:2.9.4

USER root

# Instala o Python e o pip
RUN apk add --no-cache python3 py3-pip

# Copia os scripts Python e o requirements para a pasta do n8n
COPY *.py /home/node/
COPY requirements.txt /home/node/

# Instala as bibliotecas Python
RUN pip3 install --no-cache-dir --break-system-packages -r /home/node/requirements.txt

USER node
