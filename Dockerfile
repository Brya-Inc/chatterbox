# Use the official Playwright Python image — browsers are pre-installed.
# Pin to a specific version for reproducibility.
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

# Install Python dependencies first (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Run headless by default inside the container
ENV HEADLESS=1

CMD ["python", "run_tests.py"]
