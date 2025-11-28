# 1-qadam: Asosiy bazaviy tasvir
FROM python:3.10-slim

# 2-qadam: Konvertatsiya uchun LibreOffice ni o'rnatish
# LibreOffice o'rnatish uchun zarur bo'lgan paketlar
RUN apt-get update && apt-get install -y \
    libreoffice \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# 3-qadam: Loyiha katalogini yaratish
WORKDIR /usr/src/app

# 4-qadam: Python paketlarini o'rnatish
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5-qadam: Loyiha fayllarini qo'shish
COPY . .

# 6-qadam: Konteyner ishga tushganda bajariladigan buyruq
# Uvicorn (ASGI server) yordamida ilovani ishga tushirish (Webhook xatosini hal qilish uchun)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "4"]
