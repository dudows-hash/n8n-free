FROM ghcr.io/n8n-io/n8n:1.101.1

USER root
RUN apk add --no-cache python3 py3-pip
COPY *.py /home/node/
COPY requirements.txt /home/node/
RUN pip3 install --no-cache-dir --break-system-packages -r /home/node/requirements.txt
USER node
