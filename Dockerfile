FROM python:3.13-slim

# Tesseract is optional (only needed for ?extractor=tesseract). Left out to keep
# the image small; add `tesseract-ocr` via apt-get here if you switch to the
# offline OCR path, and add pytesseract+pillow to requirements.txt.

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# The SQLite file lives on the mounted volume so the food log survives deploys.
ENV DUKE_NUTRITION_DB=/data/data.sqlite3
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
