# Shop-Floor OS demo image (tested: builds to about 174 MB, replay demo works). Build and run:
#   docker build -t shopfloor-os .
#   docker run --rm -p 8000:8000 shopfloor-os                    # replay demo: no keys, no Zetaris
#   docker run --rm -p 8000:8000 --env-file .env -e SAMPLE_MODE=false shopfloor-os   # live: in .env use
#     ZETARIS_REST_URL=http://host.docker.internal:8888/api/v1.0 and MODEL_BASE_URL=http://host.docker.internal:8088/v1
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY web ./web
COPY data/seed ./data/seed
COPY logs/fixtures ./logs/fixtures
COPY input_examples ./input_examples
COPY output_examples ./output_examples
COPY run.py AGENTS.md README.md metadata.json ./
RUN mkdir -p /app/logs
ENV SAMPLE_MODE=true PYTHONUNBUFFERED=1 PORT=8000
EXPOSE 8000
CMD ["python", "-m", "app.server", "--host", "0.0.0.0"]
