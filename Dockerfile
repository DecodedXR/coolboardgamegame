# Portability insurance (Milestone 3, decision #2). Render builds via the native
# buildpack using render.yaml; this image is the move-anywhere escape hatch
# (Fly/Railway/VPS) and is not used by Render itself.
#
# Installs ONLY the server deps and copies ONLY the server's import footprint
# (no pygame client code) to keep the image minimal.
FROM python:3.13-slim

WORKDIR /app

COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY config.py ./config.py
COPY shared/ ./shared/
COPY server/ ./server/

ENV PORT=8765
EXPOSE 8765

CMD ["python", "-m", "server"]
