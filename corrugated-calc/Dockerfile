FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "pydantic>=2.6" "uvicorn>=0.29"
COPY calc ./calc
COPY data ./data
ENV CALC_CONFIG=/app/data/seed_test.json
EXPOSE 8000
CMD ["uvicorn", "calc.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
