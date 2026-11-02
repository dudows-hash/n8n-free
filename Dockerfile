FROM himalerangana/py3-n8n:latest
FROM naskio/n8n-python:latest
FROM burakince/n8n-python:latest

USER root

RUN apk add --no-cache python3 py3-pip

COPY *.py /home/node/
COPY requirements.txt /home/node/

RUN pip3 install --no-cache-dir --break-system-packages -r /home/node/requirements.txt

USER node
