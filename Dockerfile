FROM python:3.12-slim

WORKDIR /app
COPY claude_usage_exporter.py /app/

ENV CLAUDE_PROJECTS_DIR=/projects \
    EXPORTER_PORT=9183 \
    EXPORTER_ADDR=0.0.0.0

EXPOSE 9183
ENTRYPOINT ["python3", "claude_usage_exporter.py"]
