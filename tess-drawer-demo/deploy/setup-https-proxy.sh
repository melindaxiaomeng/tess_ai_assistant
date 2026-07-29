#!/usr/bin/env bash
# ============================================================================
# Tess 测试服务器 · nginx HTTPS 反代一键脚本（自签证书）
#
# 作用：为 Tess 后端（127.0.0.1:8080）套一层 HTTPS 反代，使公网预览页
#      （HTTPS 页）能直连真后端，解除浏览器的 Mixed Content 限制。
#
# 用法（在 Tess 测试服务器上执行）：
#   sudo bash setup-https-proxy.sh
#
# 可选环境变量覆盖：
#   TESS_PROXY_PORT=8443          # 对外 HTTPS 端口（纯 IP 用非标端口避开备案检测）
#   TESS_BACKEND=127.0.0.1:8080   # 实际 Tess 服务地址
#   TESS_PUBLIC_IP=8.141.113.22    # 证书 CN / SAN 用的公网 IP
# ============================================================================
set -euo pipefail

PORT="${TESS_PROXY_PORT:-8443}"
BACKEND="${TESS_BACKEND:-127.0.0.1:8080}"
PUBLIC_IP="${TESS_PUBLIC_IP:-8.141.113.22}"
CERT_DIR=/etc/nginx/tess-ssl
DAYS=365
CONF=/etc/nginx/conf.d/tess-proxy.conf

echo "==> [1/5] 安装 nginx（已装则跳过）"
if ! command -v nginx >/dev/null 2>&1; then
  sudo dnf -y install nginx
fi

echo "==> [2/5] 生成自签证书（CN/SAN = 公网 IP，浏览器需手动信任）"
sudo mkdir -p "$CERT_DIR"
if [ ! -f "$CERT_DIR/tess.crt" ]; then
  sudo openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$CERT_DIR/tess.key" -out "$CERT_DIR/tess.crt" \
    -days "$DAYS" -subj "/CN=${PUBLIC_IP}" \
    -addext "subjectAltName=IP:${PUBLIC_IP}"
fi

echo "==> [3/5] 写入 nginx 反代配置"
sudo tee "$CONF" >/dev/null <<'NGINX_EOF'
server {
    listen __PORT__ ssl http2;
    ssl_certificate     __CERT_DIR__/tess.crt;
    ssl_certificate_key __CERT_DIR__/tess.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # CORS：公网预览页（CloudStudio 域名）跨域访问
    add_header Access-Control-Allow-Origin  "*" always;
    add_header Access-Control-Allow-Methods "*" always;
    add_header Access-Control-Allow-Headers "*" always;
    add_header Access-Control-Allow-Credentials "true" always;

    location / {
        # 预检请求直接返回 204（带上 CORS 头）
        if ($request_method = OPTIONS) {
            return 204;
        }
        proxy_pass         http://__BACKEND__;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
NGINX_EOF

# 把占位符替换为真实值（nginx 变量 $host/$request_method 等保持原样）
sudo sed -i "s|__PORT__|${PORT}|g; s|__BACKEND__|${BACKEND}|g; s|__CERT_DIR__|${CERT_DIR}|g" "$CONF"

echo "==> [4/5] 校验并重载 nginx"
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl restart nginx

echo "==> [5/5] 放行防火墙"
if command -v firewall-cmd >/dev/null 2>&1; then
  sudo firewall-cmd --permanent --add-port="${PORT}/tcp" || true
  sudo firewall-cmd --reload || true
fi

echo ""
echo "✅ HTTPS 反代已启动：https://${PUBLIC_IP}:${PORT}  ->  ${BACKEND}"
echo ""
echo "随后需要你做两件事："
echo "  1) 阿里云安全组放行 TCP ${PORT} 入站（来源建议限你本机/演示用 IP）。"
echo "  2) 在浏览器新标签页打开 https://${PUBLIC_IP}:${PORT}/healthz ，"
echo "     点「高级 → 继续访问（不安全）」手动信任自签证书；"
echo "     信任后，公网预览页即可直连真后端（演示页默认地址已改为 https://${PUBLIC_IP}:${PORT}）。"
echo ""
echo "备注：自签证书会被浏览器标记为「不安全」，属正常现象。若要无告警的受信任证书，"
echo "      需为服务器绑定域名并完成 ICP 备案，再改用 Let's Encrypt 证书（仅替换上面证书路径即可）。"
