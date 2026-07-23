# Control Design Copilot — web (React/Next) dev image (E3).
#
# In Compose the source is bind-mounted and the container command runs
# `npm install && npm run dev`, with node_modules kept in an anonymous volume so the
# host's modules never shadow the container's. This image just provides Node + a warm
# dependency cache for the first boot.
FROM node:22-slim

WORKDIR /app

# Warm the dependency layer (recreated at runtime over the bind mount, but this makes the
# first `npm install` fast and lets `docker build ./web` succeed on its own).
COPY package.json package-lock.json* ./
RUN npm install

COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev"]
