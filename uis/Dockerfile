FROM node:22-alpine

WORKDIR /app

# ---------------------------------------------------------------
# Dependency installation layer (leverages Docker cache)
# ---------------------------------------------------------------
COPY website/package.json website/package-lock.json ./uis/website/
COPY backoffice/package.json backoffice/package-lock.json ./uis/backoffice/

RUN cd /app/uis/website && npm ci
RUN cd /app/uis/backoffice && npm ci

# ---------------------------------------------------------------
# Source code layer
# ---------------------------------------------------------------
COPY website/ ./uis/website/
COPY backoffice/ ./uis/backoffice/

# ---------------------------------------------------------------
# Start script
# ---------------------------------------------------------------
COPY start.sh /uis/start.sh
RUN chmod +x /uis/start.sh

# ---------------------------------------------------------------
# Ports
# ---------------------------------------------------------------
EXPOSE 3000 3001

# ---------------------------------------------------------------
# Default command
# ---------------------------------------------------------------
CMD ["/uis/start.sh"]