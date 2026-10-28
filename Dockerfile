FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copie et installation des dépendances via requirements.txt
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copie du reste du code source
COPY . .

EXPOSE 8085
CMD ["streamlit", "run", "app.py", "--server.port=8085", "--server.address=0.0.0.0"]