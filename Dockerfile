# Shop-Floor OS demo image. Not built as part of this repo's checks; build it yourself with:
#   docker build -t shopfloor-os .
#   docker run --rm -p 8000:8000 shopfloor-os                    # replay demo: no keys, no Zetaris
#   docker run --rm -p 8000:8000 --env-file .env -e SAMPLE_MODE=false shopfloor-os   # live (needs Nightshift and Zetaris reachable)
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY web ./web
COPY data/seed ./data/seed
COPY logs/fixtures ./logs/fixtures
COPY run.py AGENTS.md README.md metadata.json ./
ENV SAMPLE_MODE=true PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "app.server", "--host", "0.0.0.0", "--port", "8000"]
