FROM python:3.10-slim
WORKDIR /app
COPY pyproject.toml .
COPY cafe_os/ ./cafe_os/
COPY tests/ ./tests/
RUN pip install -e ".[dev,prod]"
CMD ["python3", "-m", "uvicorn", "cafe_os.main:app", "--host", "0.0.0.0", "--port", "8000"]
