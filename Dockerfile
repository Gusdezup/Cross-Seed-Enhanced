FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data XS_CONFIG_DIR=/cs-config
EXPOSE 2469
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "2469", "--no-access-log"]
