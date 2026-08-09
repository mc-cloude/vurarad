FROM python:3.12-slim

# No torch, no heavyweight ML deps — slim base is sufficient
RUN groupadd -r vurarad && useradd -r -g vurarad -d /app vurarad

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

USER vurarad
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
