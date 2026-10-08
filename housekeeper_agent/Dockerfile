FROM python:3.13-alpine
RUN pip install --no-cache-dir "websockets>=13,<16"
WORKDIR /app
COPY app/ /app/
CMD ["python", "-u", "/app/agent.py"]
