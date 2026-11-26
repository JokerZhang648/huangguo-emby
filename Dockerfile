FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY bridge.py library.py source.py ./

EXPOSE 8787
CMD ["python", "bridge.py"]
