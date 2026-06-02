FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY entrypoint.sh /entrypoint.sh
COPY templates/ templates/

RUN chmod +x /entrypoint.sh

VOLUME ["/data"]
EXPOSE 5000

CMD ["/entrypoint.sh"]