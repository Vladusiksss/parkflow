FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY backend backend
COPY frontend frontend
COPY vision vision
RUN useradd --create-home parkflow && mkdir /app/data && chown parkflow:parkflow /app/data
USER parkflow
EXPOSE 8000
CMD ["uvicorn", "backend.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
