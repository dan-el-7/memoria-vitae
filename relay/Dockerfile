FROM python:3.12-slim

WORKDIR /opt/vma-relay
COPY vma_relay ./vma_relay

EXPOSE 8765
ENTRYPOINT ["python", "-m", "vma_relay.server"]

