FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OLLAMA_HOST=http://127.0.0.1:11434 \
    OLLAMA_MODEL=qwen2.5:7b-instruct

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        curl \
        poppler-utils \
        texlive-fonts-recommended \
        texlive-latex-base \
        texlive-latex-recommended \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://ollama.com/install.sh | sh

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/app /app/backend/app
COPY backend/entrypoint.sh /app/backend/entrypoint.sh
COPY resume.cls cover_letter_template.tex /app/
RUN chmod +x /app/backend/entrypoint.sh \
    && mkdir -p /app/output /app/data /config/backend /config/extension

EXPOSE 8765
ENTRYPOINT ["/app/backend/entrypoint.sh"]
CMD ["serve"]
