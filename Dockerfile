FROM python:3.13-alpine AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
LABEL org.opencontainers.image.title="lisa-sip-trigger" \
    org.opencontainers.image.source="https://github.com/pannal/lisa-sip-trigger" \
    org.opencontainers.image.description="Trigger legacy Humantechnik LISA alerts through a SIP FXS adapter" \
    org.opencontainers.image.licenses="MIT"
WORKDIR /app
COPY server.py /app/server.py
USER 65534:65534
EXPOSE 18080
HEALTHCHECK --interval=30s --timeout=3s --start-period=3s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; host=os.getenv('HTTP_BIND','0.0.0.0'); host='127.0.0.1' if host=='0.0.0.0' else host; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://'+host+':'+os.getenv('HTTP_PORT','18080')+'/health',timeout=2).read()"]

# CI executes the same tests on both container architectures.
FROM base AS test
COPY tests /app/tests
RUN python -m unittest discover -s tests -v

FROM base AS runtime
ENTRYPOINT ["python", "/app/server.py"]
