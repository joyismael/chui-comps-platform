# Chui Public Comparables Platform - container image
# Runs the Streamlit app. Suitable for Coolify or any container host.
# Provide FMP_API_KEY as an environment variable at runtime (never bake it in).
FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal; add build tools only if a wheel needs compiling.
RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Streamlit serves here; the host maps/proxies this port.
EXPOSE 8501

# FMP_API_KEY is read by the app from the environment at runtime.
ENV STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHERUSAGESTATS=false

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8501/_stcore/health').read()==b'ok' else 1)" || exit 1

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
