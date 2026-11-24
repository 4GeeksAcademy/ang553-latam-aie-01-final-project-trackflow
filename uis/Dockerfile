FROM node:22-alpine

WORKDIR /app

# ---------------------------------------------------------------
# Dependency installation layer (leverages Docker cache)
# ---------------------------------------------------------------
COPY uis/website/package.json uis/website/package-lock.json ./uis/website/
COPY uis/backoffice/package.json uis/backoffice/package-lock.json ./uis/backoffice/

RUN cd /app/uis/website && npm ci
RUN cd /app/uis/backoffice && npm ci

# ---------------------------------------------------------------
# Source code layer
# ---------------------------------------------------------------
COPY uis/website/ ./uis/website/
COPY uis/backoffice/ ./uis/backoffice/
COPY src/ ./src/

ARG API_BACKEND_URL=http://api:8000
ENV API_BACKEND_URL=${API_BACKEND_URL}
RUN cd /app/uis/backoffice && npm run build

# ---------------------------------------------------------------
# Start script
# ---------------------------------------------------------------
COPY uis/start.sh /uis/start.sh
RUN sed -i 's/\r$//' /uis/start.sh && chmod +x /uis/start.sh

# ---------------------------------------------------------------
# Ports
# ---------------------------------------------------------------
EXPOSE 3000 3001

# ---------------------------------------------------------------
# Default command
# ---------------------------------------------------------------
CMD ["/uis/start.sh"]
