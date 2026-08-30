FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    netcat-traditional \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Set working directory to Django project
WORKDIR /app/trading_system

# Collect static files
RUN python manage.py collectstatic --noinput 2>/dev/null || true

# Expose port (Railway sets PORT dynamically)
EXPOSE ${PORT:-8000}

# Copy and set entrypoint script
COPY entrypoint.sh /app/entrypoint.sh
# Normalize line endings (in case the file was authored/edited on Windows) then make executable
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Start via entrypoint script (runs migrations + creates superuser + starts Daphne)
CMD ["/app/entrypoint.sh"]
