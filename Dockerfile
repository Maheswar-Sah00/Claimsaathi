# One image for every host. APP=agent (default) or APP=portal picks one service (Railway, Render);
# on Hugging Face Spaces (SPACE_ID is set) both run in this container, the portal private on 8001.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . .

# Spaces run the container as user 1000: the database, evidence and generated vault files must be writable.
RUN mkdir -p data evidence vault_samples/seed && chmod -R a+rwX data evidence vault_samples /ms-playwright
ENV HOME=/tmp

EXPOSE 7860
CMD ["sh", "scripts/start.sh"]
