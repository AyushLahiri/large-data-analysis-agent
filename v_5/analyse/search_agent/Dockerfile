# ---- BUILD STAGE ----
FROM node:20-slim AS builder

# Set working directory
WORKDIR /app

# Copy package.json and package-lock.json
COPY package*.json ./

# Install dependencies
RUN npm install --ignore-scripts

# Copy application code
COPY . .

# Build the application
RUN npm run build --ignore-scripts

# ---- PRODUCTION STAGE ----
FROM node:20-slim AS production

# Set working directory
WORKDIR /app

# Copy package.json and package-lock.json
COPY package*.json ./

# Install production dependencies only
RUN npm install --production  --ignore-scripts

# Copy config.json and built files from builder
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates python3 poppler-utils && rm -rf /var/lib/apt/lists/*
COPY src/tools/public-page.py ./dist/tools/public-page.py
COPY --from=builder /app/config.json ./
COPY --from=builder /app/dist ./dist

# Expose the port the app runs on
RUN mkdir -p /data/research && chown node:node /data/research
EXPOSE 3000

# Set startup command
CMD ["node", "./dist/server.js"]
