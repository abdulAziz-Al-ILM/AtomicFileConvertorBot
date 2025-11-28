# Python asosini olamiz
FROM python:3.10-slim

# Kerakli tizim dasturlarini o'rnatamiz (LibreOffice va Tesseract OCR)
RUN apt-get update && apt-get install -y \
    libreoffice \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-rus \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Ishchi papkani belgilaymiz
WORKDIR /app

# Kutubxonalarni o'rnatamiz
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bot kodini ko'chiramiz
COPY . .

# Botni ishga tushiramiz
CMD ["python", "main.py"]
