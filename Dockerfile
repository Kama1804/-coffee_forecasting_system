# Use official Python 3.12 image — bypasses mise/nixpacks entirely
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (better Docker caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Create data directory for persistent volume mount
RUN mkdir -p /data

# Expose port (Railway injects $PORT at runtime)
EXPOSE 8080

# Start Flask app with gunicorn
CMD gunicorn app:app --workers 2 --threads 2 --bind "0.0.0.0:${PORT:-8080}"
