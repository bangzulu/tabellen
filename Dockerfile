FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY fp ./fp
COPY static ./static
EXPOSE 8080
# En worker håller minnet nere (tidtabellen laddas en gång); trådar räcker för trafiken.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "120", "-b", "0.0.0.0:8080", "fp.server:app"]
