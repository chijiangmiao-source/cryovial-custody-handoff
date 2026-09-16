# --- 构建阶段 ---
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
# 同源部署：浏览器访问 /api，由运行阶段的 nginx 反代到后端
RUN npm run build

# --- 运行阶段 ---
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
