FROM python:3.12-slim
# OCR за сканирани PDF и снимки (български + английски)
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-bul \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir "fastapi>=0.110" "pydantic>=2.6" "uvicorn>=0.29" "python-multipart>=0.0.9" \
    "sqlalchemy>=2.0" "alembic>=1.13" "psycopg[binary]>=3.1" "openpyxl>=3.1" "xlrd>=2.0" "pdfplumber>=0.11" \
    "pypdfium2>=4" "pillow>=10" "pillow-heif>=0.16" "pytesseract>=0.3.10"
COPY calc ./calc
COPY data ./data
COPY migrations ./migrations
COPY alembic.ini .
RUN useradd -r app && chown -R app /app
USER app
EXPOSE 8000
CMD ["sh", "-c", "python -m calc.db.cli migrate && uvicorn calc.api.app:app --host 0.0.0.0 --port 8000 --proxy-headers"]
